# Part 4 Core Trading Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-only web app (`part-4/`) to browse Polymarket markets (via
part-3's synced Postgres data), place real market-order buy/sell trades on
Polymarket, and view live balance/positions.

**Architecture:** FastAPI backend (`part-4/backend/`) holds the wallet key and all
Polymarket API access (via `py-clob-client` for orders/balance, Polymarket's Data
API for positions, Gamma API for CLOB token ids); it has a read-only Postgres
connection to part-3's `markets` table for browsing context. A React + TypeScript
SPA (`part-4/frontend/`) talks only to this backend over REST/JSON. Both run as
plain host processes (`uvicorn`, `npm run dev`/build) — no Docker, no changes to
part-3.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, psycopg (v3, matching part-3),
py-clob-client, httpx, pydantic, pytest + pytest-mock; React 18 + TypeScript + Vite,
Vitest + React Testing Library.

**Spec:** `docs/superpowers/specs/2026-08-22-part-4-trading-app-core-design.md`

## Global Constraints

- No changes to `part-3` — `part-4` only reads its Postgres `markets` table over a
  read-only connection.
- Real money: order placement always re-fetches the live price server-side before
  submitting; never trusts a client-supplied price.
- Order types: market orders only (no limit orders, no open-order book/cancel UI).
- Positions and balance are always fetched live from Polymarket — never cached or
  locally re-derived.
- The app binds to `localhost` only; no auth/login (single local user).
- Every money-moving code path (`polymarket_client.py`) is unit-tested against
  mocked responses — never against the real API in automated tests.

---

## File Structure

```
part-4/
  backend/
    app/
      __init__.py
      config.py            # Settings dataclass + load_settings(), fail-fast validation
      main.py               # FastAPI app factory, CORS, startup checks
      schemas.py            # Pydantic request/response models
      markets_repo.py       # read-only queries against part-3's markets table
      polymarket_client.py  # py-clob-client + Gamma + Data API wrapper
      routes/
        __init__.py
        markets.py          # GET /markets, GET /markets/{id}
        trading.py           # GET /account/balance, GET /account/positions, POST /orders
    tests/
      __init__.py
      test_config.py
      test_markets_repo.py       # needs TEST_DATABASE_URL, else skipped
      test_polymarket_client.py  # fully mocked, no network
      test_routes_markets.py     # FastAPI TestClient, dependencies overridden
      test_routes_trading.py     # FastAPI TestClient, dependencies overridden
    requirements.txt
    requirements-dev.txt
    pytest.ini
    .env.example
  frontend/
    src/
      api/
        client.ts           # typed fetch wrappers for the backend
        client.test.ts
      types.ts               # Market, Balance, Position, OrderRequest, OrderResult
      components/
        ConfirmOrderDialog.tsx
        ConfirmOrderDialog.test.tsx
      pages/
        MarketList.tsx
        MarketDetail.tsx
        Account.tsx
      App.tsx
      main.tsx
    index.html
    package.json
    tsconfig.json
    vite.config.ts
    .env.example
  README.md
```

---

### Task 1: Backend scaffold, config, and fail-fast startup

**Files:**
- Create: `part-4/backend/app/__init__.py`
- Create: `part-4/backend/app/config.py`
- Create: `part-4/backend/tests/__init__.py`
- Create: `part-4/backend/tests/test_config.py`
- Create: `part-4/backend/requirements.txt`
- Create: `part-4/backend/requirements-dev.txt`
- Create: `part-4/backend/pytest.ini`
- Create: `part-4/backend/.env.example`

**Interfaces:**
- Produces: `app.config.Settings` (frozen dataclass) and `app.config.load_settings()
  -> Settings`, raising `app.config.ConfigError` (a `ValueError` subclass) when a
  required variable is missing. Fields: `database_url: str`, `polymarket_private_key:
  str`, `polymarket_api_key: str`, `polymarket_api_secret: str`,
  `polymarket_api_passphrase: str`, `polymarket_chain_id: int`, `clob_host: str`,
  `gamma_markets_url: str`, `data_api_url: str`, `max_slippage_pct: float`,
  `cors_origin: str`, `port: int`.

- [ ] **Step 1: Write the failing test for missing required config**

```python
# part-4/backend/tests/test_config.py
import pytest

from app.config import ConfigError, load_settings


def test_missing_private_key_raises(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://pm:pm@localhost:5432/pm")
    monkeypatch.setenv("POLYMARKET_API_KEY", "k")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "s")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "p")
    with pytest.raises(ConfigError, match="POLYMARKET_PRIVATE_KEY"):
        load_settings()


def test_defaults_applied(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pm:pm@localhost:5432/pm")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("POLYMARKET_API_KEY", "k")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "s")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "p")
    settings = load_settings()
    assert settings.polymarket_chain_id == 137
    assert settings.clob_host == "https://clob.polymarket.com"
    assert settings.max_slippage_pct == 0.02
    assert settings.port == 8420
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'` (module doesn't exist yet).

- [ ] **Step 3: Write `app/__init__.py`, `tests/__init__.py`, and `app/config.py`**

```python
# part-4/backend/app/__init__.py
```

```python
# part-4/backend/tests/__init__.py
```

```python
# part-4/backend/app/config.py
"""Backend configuration loaded from environment variables.

Every required value (wallet key, CLOB API creds, DB connection) must be set
explicitly — there is no safe default for credentials that move real money.
"""
import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when a required environment variable is missing at startup."""


_REQUIRED = (
    "DATABASE_URL",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)


@dataclass(frozen=True)
class Settings:
    database_url: str
    polymarket_private_key: str
    polymarket_api_key: str
    polymarket_api_secret: str
    polymarket_api_passphrase: str
    polymarket_chain_id: int
    clob_host: str
    gamma_markets_url: str
    data_api_url: str
    max_slippage_pct: float
    cors_origin: str
    port: int


def load_settings() -> Settings:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        polymarket_private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        polymarket_api_key=os.environ["POLYMARKET_API_KEY"],
        polymarket_api_secret=os.environ["POLYMARKET_API_SECRET"],
        polymarket_api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
        polymarket_chain_id=int(os.environ.get("POLYMARKET_CHAIN_ID", "137")),
        clob_host=os.environ.get("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_markets_url=os.environ.get(
            "GAMMA_MARKETS_URL", "https://gamma-api.polymarket.com/markets"
        ),
        data_api_url=os.environ.get(
            "DATA_API_URL", "https://data-api.polymarket.com"
        ),
        max_slippage_pct=float(os.environ.get("MAX_SLIPPAGE_PCT", "0.02")),
        cors_origin=os.environ.get("CORS_ORIGIN", "http://localhost:5173"),
        port=int(os.environ.get("PORT", "8420")),
    )
```

```ini
# part-4/backend/pytest.ini
[pytest]
testpaths = tests
```

```
# part-4/backend/requirements.txt
fastapi
uvicorn[standard]
psycopg[binary]
py-clob-client
httpx
pydantic
python-dotenv
```

```
# part-4/backend/requirements-dev.txt
pytest
pytest-mock
```

```
# part-4/backend/.env.example
# --- database (read-only role against part-3's Postgres) ---
DATABASE_URL=postgresql://pm_readonly:pm@localhost:5432/pm

# --- Polymarket wallet + CLOB API credentials ---
# Never commit the real .env. This app moves real money.
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_CHAIN_ID=137

# --- optional tuning (defaults shown) ---
# CLOB_HOST=https://clob.polymarket.com
# GAMMA_MARKETS_URL=https://gamma-api.polymarket.com/markets
# DATA_API_URL=https://data-api.polymarket.com
# MAX_SLIPPAGE_PCT=0.02
# CORS_ORIGIN=http://localhost:5173
# PORT=8420
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/backend && python -m pytest tests/test_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/__init__.py part-4/backend/app/config.py \
        part-4/backend/tests/__init__.py part-4/backend/tests/test_config.py \
        part-4/backend/requirements.txt part-4/backend/requirements-dev.txt \
        part-4/backend/pytest.ini part-4/backend/.env.example
git commit -m "part-4: add backend scaffold with fail-fast config loading"
```

---

### Task 2: Pydantic schemas

**Files:**
- Create: `part-4/backend/app/schemas.py`

**Interfaces:**
- Consumes: nothing (pure data models).
- Produces: `MarketSummary`, `MarketDetail`, `Balance`, `Position`, `OrderRequest`,
  `OrderResult`, `Side` (`"BUY"` / `"SELL"`), `Outcome` (`"YES"` / `"NO"`) — used by
  `markets_repo`, `polymarket_client`, and all routes in later tasks.

- [ ] **Step 1: Write the failing test**

```python
# part-4/backend/tests/test_schemas.py
import pytest
from pydantic import ValidationError

from app.schemas import OrderRequest


def test_order_request_rejects_non_positive_size():
    with pytest.raises(ValidationError):
        OrderRequest(market_id="1", outcome="YES", side="BUY", usdc_size=0)


def test_order_request_accepts_valid_payload():
    req = OrderRequest(market_id="1", outcome="YES", side="BUY", usdc_size=5.0)
    assert req.usdc_size == 5.0
    assert req.outcome == "YES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas'`

- [ ] **Step 3: Write `app/schemas.py`**

```python
# part-4/backend/app/schemas.py
"""Request/response models shared across routes.

`OrderRequest` is the one payload that moves money — its validation is the
first line of defense against a malformed trade.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field

Side = Literal["BUY", "SELL"]
Outcome = Literal["YES", "NO"]


class MarketSummary(BaseModel):
    id: str
    question: str
    slug: Optional[str]
    end_date: Optional[str]
    current_score: Optional[float]
    volume_24h: Optional[float]
    liquidity: Optional[float]


class MarketDetail(MarketSummary):
    description: str
    live_yes_price: float


class OrderRequest(BaseModel):
    market_id: str
    outcome: Outcome
    side: Side
    usdc_size: float = Field(gt=0)


class OrderResult(BaseModel):
    order_id: str
    market_id: str
    outcome: Outcome
    side: Side
    executed_price: float
    usdc_size: float


class Position(BaseModel):
    market_id: str
    outcome: Outcome
    size: float
    average_price: float
    current_value: float


class Balance(BaseModel):
    usdc_available: float
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/backend && python -m pytest tests/test_schemas.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/schemas.py part-4/backend/tests/test_schemas.py
git commit -m "part-4: add shared Pydantic schemas"
```

---

### Task 3: `markets_repo.py` — read-only market queries

**Files:**
- Create: `part-4/backend/app/markets_repo.py`
- Create: `part-4/backend/tests/test_markets_repo.py`

**Interfaces:**
- Consumes: `app.schemas.MarketSummary`.
- Produces: `class MarketsRepo` with `__init__(self, database_url: str)`,
  `list_markets(self, min_price: float = 0.0, max_price: float = 1.0, limit: int =
  100) -> list[MarketSummary]`, `get_market(self, market_id: str) ->
  Optional[MarketSummary]` (adds `description: str` and `slug`), `close(self) ->
  None`. Row order: `current_score` may be `NULL`; treat as excluded from the price
  filter (a market with no belief yet is still browsable, just not price-filtered).

- [ ] **Step 1: Write the failing test**

```python
# part-4/backend/tests/test_markets_repo.py
"""Integration tests against a real Postgres. Skipped unless TEST_DATABASE_URL is
set, matching part-3's existing pattern (see part-3/pytest.ini / test suite)."""
import os
import uuid

import psycopg
import pytest
from pgvector.psycopg import register_vector

from app.markets_repo import MarketsRepo

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set"
)


@pytest.fixture
def repo():
    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    register_vector(conn)
    market_id = f"test-{uuid.uuid4()}"
    conn.execute(
        """
        INSERT INTO markets (id, question, description, current_score, slug,
                              volume_24h, liquidity, closed, embedding)
        VALUES (%s, %s, '', 0.85, %s, 1000, 5000, FALSE, %s)
        """,
        (market_id, "Will X happen?", "test-slug", [0.0] * 1024),
    )
    yield MarketsRepo(TEST_DATABASE_URL), market_id
    conn.execute("DELETE FROM markets WHERE id = %s", (market_id,))
    conn.close()


def test_list_markets_filters_by_price_band(repo):
    repo_obj, market_id = repo
    results = repo_obj.list_markets(min_price=0.8, max_price=0.9)
    assert any(m.id == market_id for m in results)
    results_excluded = repo_obj.list_markets(min_price=0.0, max_price=0.5)
    assert not any(m.id == market_id for m in results_excluded)
    repo_obj.close()


def test_get_market_returns_none_for_missing_id(repo):
    repo_obj, _ = repo
    assert repo_obj.get_market("does-not-exist") is None
    repo_obj.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_markets_repo.py -v`
Expected: without `TEST_DATABASE_URL` set, SKIPPED. With it set against a part-3
Postgres, FAIL with `ModuleNotFoundError: No module named 'app.markets_repo'`.

- [ ] **Step 3: Write `app/markets_repo.py`**

```python
# part-4/backend/app/markets_repo.py
"""Read-only access to part-3's `markets` table.

part-4 never writes to this table — it only browses what the syncer already
populated. Connect with a read-only Postgres role in production (see
.env.example); nothing here issues a write statement.
"""
from typing import Optional

import psycopg

from app.schemas import MarketDetail, MarketSummary


class MarketsRepo:
    def __init__(self, database_url: str):
        self._conn = psycopg.connect(database_url, autocommit=True)

    def close(self) -> None:
        self._conn.close()

    def list_markets(
        self, min_price: float = 0.0, max_price: float = 1.0, limit: int = 100
    ) -> list[MarketSummary]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, question, slug, end_date, current_score,
                       volume_24h, liquidity
                FROM markets
                WHERE NOT closed
                  AND (current_score IS NULL
                       OR current_score BETWEEN %s AND %s)
                ORDER BY volume_24h DESC NULLS LAST
                LIMIT %s
                """,
                (min_price, max_price, limit),
            )
            return [
                MarketSummary(
                    id=row[0],
                    question=row[1],
                    slug=row[2],
                    end_date=row[3].isoformat() if row[3] else None,
                    current_score=row[4],
                    volume_24h=row[5],
                    liquidity=row[6],
                )
                for row in cur.fetchall()
            ]

    def get_market(self, market_id: str) -> Optional[MarketDetail]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, question, description, slug, end_date, current_score,
                       volume_24h, liquidity
                FROM markets
                WHERE id = %s
                """,
                (market_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return MarketDetail(
                id=row[0],
                question=row[1],
                description=row[2],
                slug=row[3],
                end_date=row[4].isoformat() if row[4] else None,
                current_score=row[5],
                volume_24h=row[6],
                liquidity=row[7],
                live_yes_price=row[5] if row[5] is not None else 0.5,
            )
```

Note: `get_market`'s `live_yes_price` is a placeholder value here — Task 6's route
overwrites it with a real live price from `polymarket_client` before returning to
the frontend. `MarketsRepo` itself only ever reads the DB.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/backend && TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm python -m pytest tests/test_markets_repo.py -v`
Expected: PASS (2 tests) against a running part-3 Postgres; SKIPPED otherwise.

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/markets_repo.py part-4/backend/tests/test_markets_repo.py
git commit -m "part-4: add read-only markets_repo against part-3's Postgres"
```

---

### Task 4: `polymarket_client.py` — balance and positions

**Files:**
- Create: `part-4/backend/app/polymarket_client.py`
- Create: `part-4/backend/tests/test_polymarket_client.py`

**Interfaces:**
- Consumes: `app.config.Settings`, `app.schemas.Balance`, `app.schemas.Position`.
- Produces: `class PolymarketClient` with `__init__(self, settings: Settings)`,
  `get_balance(self) -> Balance`, `get_positions(self) -> list[Position]`. Internally
  builds a wallet address from the private key (needed for the Data API positions
  call) via `self._address: str`.

This task covers balance/positions only; price lookup, token-id lookup, and order
placement are Tasks 5 and 6 on the same class.

- [ ] **Step 1: Write the failing test**

```python
# part-4/backend/tests/test_polymarket_client.py
"""All Polymarket network calls are mocked — this suite never hits the real API."""
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.polymarket_client import PolymarketClient


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql://x",
        polymarket_private_key="0x" + "1" * 64,
        polymarket_api_key="k",
        polymarket_api_secret="s",
        polymarket_api_passphrase="p",
        polymarket_chain_id=137,
        clob_host="https://clob.polymarket.com",
        gamma_markets_url="https://gamma-api.polymarket.com/markets",
        data_api_url="https://data-api.polymarket.com",
        max_slippage_pct=0.02,
        cors_origin="http://localhost:5173",
        port=8420,
    )


@patch("app.polymarket_client.ClobClient")
def test_get_balance_parses_usdc_available(mock_clob_cls, settings):
    mock_client = MagicMock()
    mock_client.get_balance_allowance.return_value = {"balance": "12500000"}
    mock_clob_cls.return_value = mock_client

    client = PolymarketClient(settings)
    balance = client.get_balance()

    assert balance.usdc_available == pytest.approx(12.5)


@patch("app.polymarket_client.httpx.Client.get")
@patch("app.polymarket_client.ClobClient")
def test_get_positions_parses_data_api_response(mock_clob_cls, mock_get, settings):
    mock_clob_cls.return_value = MagicMock()
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [
            {
                "conditionId": "0xmarket1",
                "outcome": "Yes",
                "size": 10.0,
                "avgPrice": 0.6,
                "currentValue": 7.0,
            }
        ],
    )

    client = PolymarketClient(settings)
    positions = client.get_positions()

    assert len(positions) == 1
    assert positions[0].market_id == "0xmarket1"
    assert positions[0].outcome == "YES"
    assert positions[0].size == 10.0
    assert positions[0].average_price == 0.6
    assert positions[0].current_value == 7.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_polymarket_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.polymarket_client'`

- [ ] **Step 3: Write `app/polymarket_client.py` (balance + positions only)**

```python
# part-4/backend/app/polymarket_client.py
"""The one place that touches funds: wraps py-clob-client (orders, balance) and
Polymarket's Data API (positions, keyed by wallet address).

Every method here does a real network call — there is no local caching of
money-relevant state, by design (see spec: positions/balance are always live).
"""
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
import httpx

from app.config import Settings
from app.schemas import Balance, Position

_USDC_DECIMALS = 6


class PolymarketClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._address = Account.from_key(settings.polymarket_private_key).address
        self._clob = ClobClient(
            host=settings.clob_host,
            key=settings.polymarket_private_key,
            chain_id=settings.polymarket_chain_id,
            creds=ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase,
            ),
        )
        self._http = httpx.Client(timeout=15.0)

    def get_balance(self) -> Balance:
        raw = self._clob.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        usdc = float(raw["balance"]) / (10 ** _USDC_DECIMALS)
        return Balance(usdc_available=usdc)

    def get_positions(self) -> list[Position]:
        resp = self._http.get(
            f"{self._settings.data_api_url}/positions",
            params={"user": self._address},
        )
        resp.raise_for_status()
        return [
            Position(
                market_id=row["conditionId"],
                outcome="YES" if row["outcome"].upper() == "YES" else "NO",
                size=float(row["size"]),
                average_price=float(row["avgPrice"]),
                current_value=float(row["currentValue"]),
            )
            for row in resp.json()
        ]
```

- [ ] **Step 4: Add `eth-account` to requirements and run the tests**

```
# part-4/backend/requirements.txt  (append)
eth-account
```

Run: `cd part-4/backend && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest tests/test_polymarket_client.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/polymarket_client.py part-4/backend/tests/test_polymarket_client.py \
        part-4/backend/requirements.txt
git commit -m "part-4: add PolymarketClient balance and positions"
```

---

### Task 5: `polymarket_client.py` — live price and CLOB token id lookup

**Files:**
- Modify: `part-4/backend/app/polymarket_client.py`
- Modify: `part-4/backend/tests/test_polymarket_client.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PolymarketClient.get_token_ids(self, market_id: str) -> dict[str, str]`
  (returns `{"YES": token_id, "NO": token_id}`, fetched from Gamma by market id) and
  `PolymarketClient.get_live_price(self, token_id: str) -> float` (via the CLOB
  client's order-book midpoint). Task 6 (order placement) and Task 7 (routes) both
  call these.

- [ ] **Step 1: Write the failing tests**

```python
# part-4/backend/tests/test_polymarket_client.py  (append)
@patch("app.polymarket_client.httpx.Client.get")
@patch("app.polymarket_client.ClobClient")
def test_get_token_ids_parses_gamma_response(mock_clob_cls, mock_get, settings):
    mock_clob_cls.return_value = MagicMock()
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [
            {
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["111", "222"]',
            }
        ],
    )

    client = PolymarketClient(settings)
    token_ids = client.get_token_ids("0xmarket1")

    assert token_ids == {"YES": "111", "NO": "222"}


@patch("app.polymarket_client.ClobClient")
def test_get_live_price_reads_midpoint(mock_clob_cls, settings):
    mock_client = MagicMock()
    mock_client.get_midpoint.return_value = {"mid": "0.63"}
    mock_clob_cls.return_value = mock_client

    client = PolymarketClient(settings)
    price = client.get_live_price("111")

    assert price == pytest.approx(0.63)
    mock_client.get_midpoint.assert_called_once_with("111")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_polymarket_client.py -v`
Expected: FAIL — `AttributeError: 'PolymarketClient' object has no attribute
'get_token_ids'`

- [ ] **Step 3: Add the two methods**

```python
# part-4/backend/app/polymarket_client.py  (add imports + methods)
import json
```

```python
    # --- add inside class PolymarketClient ---

    def get_token_ids(self, market_id: str) -> dict[str, str]:
        """{"YES": token_id, "NO": token_id} for a market, fetched from Gamma.

        part-3's `markets` table doesn't store CLOB token ids (it's a read-only
        dependency we don't get to alter), so this is looked up live at trade
        time rather than synced ahead of time.
        """
        resp = self._http.get(
            self._settings.gamma_markets_url, params={"id": market_id}
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise ValueError(f"market {market_id} not found on Gamma")
        row = rows[0]
        outcomes = json.loads(row["outcomes"])
        token_ids = json.loads(row["clobTokenIds"])
        mapping = {o.upper(): t for o, t in zip(outcomes, token_ids)}
        return {"YES": mapping["YES"], "NO": mapping["NO"]}

    def get_live_price(self, token_id: str) -> float:
        midpoint = self._clob.get_midpoint(token_id)
        return float(midpoint["mid"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/backend && python -m pytest tests/test_polymarket_client.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/polymarket_client.py part-4/backend/tests/test_polymarket_client.py
git commit -m "part-4: add live price and CLOB token id lookup"
```

---

### Task 6: `polymarket_client.py` — market order placement with slippage guard

**Files:**
- Modify: `part-4/backend/app/polymarket_client.py`
- Modify: `part-4/backend/tests/test_polymarket_client.py`

**Interfaces:**
- Consumes: `get_live_price`, `get_token_ids` (Task 5), `app.schemas.OrderRequest`,
  `app.schemas.OrderResult`.
- Produces: `PolymarketClient.place_market_order(self, order: OrderRequest) ->
  OrderResult`, raising `app.polymarket_client.SlippageExceeded` (a `RuntimeError`
  subclass) when the fill price would exceed `settings.max_slippage_pct` versus the
  price quoted immediately before submission. Route layer (Task 7) maps this to an
  HTTP 409.

- [ ] **Step 1: Write the failing tests**

```python
# part-4/backend/tests/test_polymarket_client.py  (append)
from app.polymarket_client import SlippageExceeded
from app.schemas import OrderRequest


@patch("app.polymarket_client.ClobClient")
def test_place_market_order_submits_and_returns_result(mock_clob_cls, settings):
    mock_client = MagicMock()
    mock_client.get_midpoint.return_value = {"mid": "0.60"}
    mock_client.create_market_order.return_value = {"orderID": "order-1"}
    mock_client.post_order.return_value = {
        "success": True,
        "orderID": "order-1",
        "makingAmount": "5.0",
        "price": "0.605",
    }
    mock_clob_cls.return_value = mock_client

    client = PolymarketClient(settings)
    client.get_token_ids = MagicMock(return_value={"YES": "111", "NO": "222"})

    order = OrderRequest(market_id="m1", outcome="YES", side="BUY", usdc_size=5.0)
    result = client.place_market_order(order)

    assert result.order_id == "order-1"
    assert result.executed_price == pytest.approx(0.605)
    mock_client.create_market_order.assert_called_once()
    mock_client.post_order.assert_called_once()


@patch("app.polymarket_client.ClobClient")
def test_place_market_order_raises_on_excess_slippage(mock_clob_cls, settings):
    mock_client = MagicMock()
    mock_client.get_midpoint.side_effect = [
        {"mid": "0.60"},  # quote used for the slippage bound
    ]
    mock_client.create_market_order.return_value = {"orderID": "order-1"}
    mock_client.post_order.return_value = {
        "success": True,
        "orderID": "order-1",
        "makingAmount": "5.0",
        "price": "0.90",  # far worse than the 0.60 quote, beyond 2% slippage
    }
    mock_clob_cls.return_value = mock_client

    client = PolymarketClient(settings)
    client.get_token_ids = MagicMock(return_value={"YES": "111", "NO": "222"})

    order = OrderRequest(market_id="m1", outcome="YES", side="BUY", usdc_size=5.0)
    with pytest.raises(SlippageExceeded):
        client.place_market_order(order)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_polymarket_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'SlippageExceeded'`

- [ ] **Step 3: Add `place_market_order`**

```python
# part-4/backend/app/polymarket_client.py  (add near top, after imports)
from py_clob_client.clob_types import MarketOrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

from app.schemas import OrderRequest, OrderResult


class SlippageExceeded(RuntimeError):
    """The fill price moved past the configured max slippage before submission."""
```

```python
    # --- add inside class PolymarketClient ---

    def place_market_order(self, order: OrderRequest) -> OrderResult:
        token_ids = self.get_token_ids(order.market_id)
        token_id = token_ids[order.outcome]
        quoted_price = self.get_live_price(token_id)

        side = BUY if order.side == "BUY" else SELL
        market_order = self._clob.create_market_order(
            MarketOrderArgs(token_id=token_id, amount=order.usdc_size, side=side)
        )
        response = self._clob.post_order(market_order)

        executed_price = float(response["price"])
        deviation = abs(executed_price - quoted_price) / quoted_price
        if deviation > self._settings.max_slippage_pct:
            raise SlippageExceeded(
                f"fill price {executed_price} deviated {deviation:.1%} from the "
                f"{quoted_price} quote, exceeding the "
                f"{self._settings.max_slippage_pct:.1%} limit"
            )

        return OrderResult(
            order_id=response["orderID"],
            market_id=order.market_id,
            outcome=order.outcome,
            side=order.side,
            executed_price=executed_price,
            usdc_size=float(response["makingAmount"]),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/backend && python -m pytest tests/test_polymarket_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/polymarket_client.py part-4/backend/tests/test_polymarket_client.py
git commit -m "part-4: add market order placement with post-fill slippage guard"
```

---

### Task 7: Routes — markets

**Files:**
- Create: `part-4/backend/app/routes/__init__.py`
- Create: `part-4/backend/app/routes/markets.py`
- Create: `part-4/backend/tests/test_routes_markets.py`

**Interfaces:**
- Consumes: `MarketsRepo.list_markets`, `MarketsRepo.get_market`,
  `PolymarketClient.get_live_price`, `PolymarketClient.get_token_ids`.
- Produces: `router` (a `fastapi.APIRouter`) exposing `GET /markets` ->
  `list[MarketSummary]` and `GET /markets/{market_id}` -> `MarketDetail` (404 if
  missing). Wired into the app in Task 9 via
  `app.dependencies.get_markets_repo`/`get_polymarket_client` overrides.

- [ ] **Step 1: Write the failing test**

```python
# part-4/backend/tests/test_routes_markets.py
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.main import app, get_markets_repo, get_polymarket_client
from app.schemas import MarketDetail, MarketSummary


def _override_deps(repo: MagicMock, client: MagicMock):
    app.dependency_overrides[get_markets_repo] = lambda: repo
    app.dependency_overrides[get_polymarket_client] = lambda: client


def test_list_markets_returns_repo_results():
    repo = MagicMock()
    repo.list_markets.return_value = [
        MarketSummary(
            id="1", question="Q?", slug="q", end_date=None,
            current_score=0.7, volume_24h=1000, liquidity=2000,
        )
    ]
    _override_deps(repo, MagicMock())
    with TestClient(app) as test_client:
        resp = test_client.get("/markets")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "1"
    app.dependency_overrides.clear()


def test_get_market_merges_live_price():
    repo = MagicMock()
    repo.get_market.return_value = MarketDetail(
        id="1", question="Q?", description="d", slug="q", end_date=None,
        current_score=0.7, volume_24h=1000, liquidity=2000, live_yes_price=0.5,
    )
    client = MagicMock()
    client.get_token_ids.return_value = {"YES": "111", "NO": "222"}
    client.get_live_price.return_value = 0.72
    _override_deps(repo, client)
    with TestClient(app) as test_client:
        resp = test_client.get("/markets/1")
    assert resp.status_code == 200
    assert resp.json()["live_yes_price"] == 0.72
    app.dependency_overrides.clear()


def test_get_market_404_when_missing():
    repo = MagicMock()
    repo.get_market.return_value = None
    _override_deps(repo, MagicMock())
    with TestClient(app) as test_client:
        resp = test_client.get("/markets/does-not-exist")
    assert resp.status_code == 404
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_routes_markets.py -v`
Expected: FAIL — `app.main` doesn't exist yet (`ModuleNotFoundError`).

- [ ] **Step 3: Write `app/routes/__init__.py`, `app/routes/markets.py`, and a
  minimal `app/main.py` with the two dependency providers**

```python
# part-4/backend/app/routes/__init__.py
```

```python
# part-4/backend/app/routes/markets.py
from fastapi import APIRouter, Depends, HTTPException

from app.markets_repo import MarketsRepo
from app.polymarket_client import PolymarketClient
from app.schemas import MarketDetail, MarketSummary

router = APIRouter()


def _deps():
    # Imported lazily to avoid a circular import with app.main, which defines
    # the actual dependency-injected providers.
    from app.main import get_markets_repo, get_polymarket_client

    return get_markets_repo, get_polymarket_client


@router.get("/markets", response_model=list[MarketSummary])
def list_markets(
    min_price: float = 0.0,
    max_price: float = 1.0,
    repo: MarketsRepo = Depends(lambda: _deps()[0]()),
):
    return repo.list_markets(min_price=min_price, max_price=max_price)


@router.get("/markets/{market_id}", response_model=MarketDetail)
def get_market(
    market_id: str,
    repo: MarketsRepo = Depends(lambda: _deps()[0]()),
    client: PolymarketClient = Depends(lambda: _deps()[1]()),
):
    market = repo.get_market(market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    token_ids = client.get_token_ids(market_id)
    market.live_yes_price = client.get_live_price(token_ids["YES"])
    return market
```

```python
# part-4/backend/app/main.py
from fastapi import FastAPI

from app.config import load_settings
from app.markets_repo import MarketsRepo
from app.polymarket_client import PolymarketClient
from app.routes.markets import router as markets_router

app = FastAPI(title="part-4 trading app")
app.include_router(markets_router)

_settings = None
_markets_repo = None
_polymarket_client = None


def get_markets_repo() -> MarketsRepo:
    global _settings, _markets_repo
    if _markets_repo is None:
        _settings = _settings or load_settings()
        _markets_repo = MarketsRepo(_settings.database_url)
    return _markets_repo


def get_polymarket_client() -> PolymarketClient:
    global _settings, _polymarket_client
    if _polymarket_client is None:
        _settings = _settings or load_settings()
        _polymarket_client = PolymarketClient(_settings)
    return _polymarket_client
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/backend && python -m pytest tests/test_routes_markets.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/routes/__init__.py part-4/backend/app/routes/markets.py \
        part-4/backend/app/main.py part-4/backend/tests/test_routes_markets.py
git commit -m "part-4: add GET /markets and GET /markets/{id} routes"
```

---

### Task 8: Routes — trading (balance, positions, place order)

**Files:**
- Create: `part-4/backend/app/routes/trading.py`
- Create: `part-4/backend/tests/test_routes_trading.py`
- Modify: `part-4/backend/app/main.py`

**Interfaces:**
- Consumes: `PolymarketClient.get_balance`, `.get_positions`,
  `.place_market_order`, `app.polymarket_client.SlippageExceeded`.
- Produces: `router` exposing `GET /account/balance` -> `Balance`, `GET
  /account/positions` -> `list[Position]`, `POST /orders` (body: `OrderRequest`) ->
  `OrderResult` (409 on `SlippageExceeded`, 502 on any other Polymarket error).

- [ ] **Step 1: Write the failing test**

```python
# part-4/backend/tests/test_routes_trading.py
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.main import app, get_polymarket_client
from app.polymarket_client import SlippageExceeded
from app.schemas import Balance, OrderResult, Position


def _override(client: MagicMock):
    app.dependency_overrides[get_polymarket_client] = lambda: client


def test_get_balance():
    client = MagicMock()
    client.get_balance.return_value = Balance(usdc_available=42.5)
    _override(client)
    with TestClient(app) as tc:
        resp = tc.get("/account/balance")
    assert resp.status_code == 200
    assert resp.json()["usdc_available"] == 42.5
    app.dependency_overrides.clear()


def test_get_positions():
    client = MagicMock()
    client.get_positions.return_value = [
        Position(market_id="1", outcome="YES", size=10, average_price=0.5,
                  current_value=6.0)
    ]
    _override(client)
    with TestClient(app) as tc:
        resp = tc.get("/account/positions")
    assert resp.status_code == 200
    assert resp.json()[0]["market_id"] == "1"
    app.dependency_overrides.clear()


def test_place_order_success():
    client = MagicMock()
    client.place_market_order.return_value = OrderResult(
        order_id="o1", market_id="1", outcome="YES", side="BUY",
        executed_price=0.6, usdc_size=5.0,
    )
    _override(client)
    with TestClient(app) as tc:
        resp = tc.post(
            "/orders",
            json={"market_id": "1", "outcome": "YES", "side": "BUY", "usdc_size": 5.0},
        )
    assert resp.status_code == 200
    assert resp.json()["order_id"] == "o1"
    app.dependency_overrides.clear()


def test_place_order_slippage_returns_409():
    client = MagicMock()
    client.place_market_order.side_effect = SlippageExceeded("too much slippage")
    _override(client)
    with TestClient(app) as tc:
        resp = tc.post(
            "/orders",
            json={"market_id": "1", "outcome": "YES", "side": "BUY", "usdc_size": 5.0},
        )
    assert resp.status_code == 409
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_routes_trading.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routes.trading'`

- [ ] **Step 3: Write `app/routes/trading.py` and wire it into `app/main.py`**

```python
# part-4/backend/app/routes/trading.py
from fastapi import APIRouter, Depends, HTTPException

from app.polymarket_client import PolymarketClient, SlippageExceeded
from app.schemas import Balance, OrderRequest, OrderResult, Position

router = APIRouter()


def _client_dep():
    from app.main import get_polymarket_client

    return get_polymarket_client()


@router.get("/account/balance", response_model=Balance)
def get_balance(client: PolymarketClient = Depends(_client_dep)):
    return client.get_balance()


@router.get("/account/positions", response_model=list[Position])
def get_positions(client: PolymarketClient = Depends(_client_dep)):
    return client.get_positions()


@router.post("/orders", response_model=OrderResult)
def place_order(
    order: OrderRequest, client: PolymarketClient = Depends(_client_dep)
):
    try:
        return client.place_market_order(order)
    except SlippageExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # any other Polymarket/network failure
        raise HTTPException(status_code=502, detail=str(exc))
```

```python
# part-4/backend/app/main.py  (modify)
from app.routes.trading import router as trading_router
# ... after app.include_router(markets_router):
app.include_router(trading_router)
```

Also fix `app/routes/markets.py`'s dependency wiring to match the simpler
`_client_dep` pattern used above (avoids the earlier `_deps()` indirection):

```python
# part-4/backend/app/routes/markets.py  (replace the _deps()-based Depends calls)
def _repo_dep():
    from app.main import get_markets_repo

    return get_markets_repo()


def _client_dep():
    from app.main import get_polymarket_client

    return get_polymarket_client()


@router.get("/markets", response_model=list[MarketSummary])
def list_markets(
    min_price: float = 0.0,
    max_price: float = 1.0,
    repo: MarketsRepo = Depends(_repo_dep),
):
    return repo.list_markets(min_price=min_price, max_price=max_price)


@router.get("/markets/{market_id}", response_model=MarketDetail)
def get_market(
    market_id: str,
    repo: MarketsRepo = Depends(_repo_dep),
    client: PolymarketClient = Depends(_client_dep),
):
    market = repo.get_market(market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    token_ids = client.get_token_ids(market_id)
    market.live_yes_price = client.get_live_price(token_ids["YES"])
    return market
```

(Delete the now-unused `_deps()` helper in that file.)

- [ ] **Step 4: Run all backend tests to verify everything passes**

Run: `cd part-4/backend && python -m pytest tests/ -v`
Expected: PASS on all tests (routes + client + repo [skipped without
`TEST_DATABASE_URL`] + config + schemas).

- [ ] **Step 5: Commit**

```bash
git add part-4/backend/app/routes/trading.py part-4/backend/app/routes/markets.py \
        part-4/backend/app/main.py part-4/backend/tests/test_routes_trading.py
git commit -m "part-4: add account balance/positions and order placement routes"
```

---

### Task 9: CORS, startup validation, and run instructions

**Files:**
- Modify: `part-4/backend/app/main.py`
- Create: `part-4/README.md`

**Interfaces:**
- Consumes: `app.config.load_settings`, `app.config.ConfigError`.
- Produces: app now fails fast (raises `ConfigError`, exits nonzero) at process
  start if required env vars are missing, rather than deferring the failure to the
  first request; CORS restricted to `settings.cors_origin`.

- [ ] **Step 1: Write the failing test**

```python
# part-4/backend/tests/test_main_startup.py
import pytest

from app.config import ConfigError


def test_load_settings_called_eagerly_raises_without_env(monkeypatch):
    for var in (
        "DATABASE_URL", "POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
    ):
        monkeypatch.delenv(var, raising=False)

    import importlib

    import app.main as main_module

    with pytest.raises(ConfigError):
        importlib.reload(main_module)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/backend && python -m pytest tests/test_main_startup.py -v`
Expected: FAIL — no error raised, because `app/main.py` currently calls
`load_settings()` lazily inside the dependency providers, not at import time.

- [ ] **Step 3: Make `app/main.py` load settings eagerly and add CORS**

```python
# part-4/backend/app/main.py  (rewrite)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import load_settings
from app.markets_repo import MarketsRepo
from app.polymarket_client import PolymarketClient
from app.routes.markets import router as markets_router
from app.routes.trading import router as trading_router

_settings = load_settings()  # fail fast: crash at import time, not first request

app = FastAPI(title="part-4 trading app")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_settings.cors_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(markets_router)
app.include_router(trading_router)

_markets_repo = None
_polymarket_client = None


def get_markets_repo() -> MarketsRepo:
    global _markets_repo
    if _markets_repo is None:
        _markets_repo = MarketsRepo(_settings.database_url)
    return _markets_repo


def get_polymarket_client() -> PolymarketClient:
    global _polymarket_client
    if _polymarket_client is None:
        _polymarket_client = PolymarketClient(_settings)
    return _polymarket_client
```

Note: because `app/main.py` now loads settings at import time, every other test
file that imports `app.main` (Tasks 7-8) needs the required env vars present in
the test environment. Add a `conftest.py` so they're set before any test module
imports `app.main`:

```python
# part-4/backend/tests/conftest.py
import os

os.environ.setdefault("DATABASE_URL", "postgresql://pm:pm@localhost:5432/pm")
os.environ.setdefault("POLYMARKET_PRIVATE_KEY", "0x" + "1" * 64)
os.environ.setdefault("POLYMARKET_API_KEY", "test-key")
os.environ.setdefault("POLYMARKET_API_SECRET", "test-secret")
os.environ.setdefault("POLYMARKET_API_PASSPHRASE", "test-passphrase")
```

- [ ] **Step 4: Run all backend tests to verify everything passes**

Run: `cd part-4/backend && python -m pytest tests/ -v`
Expected: PASS on all tests, including the new startup test (which explicitly
`monkeypatch.delenv`s before reloading, overriding the `conftest.py` defaults for
that one test).

- [ ] **Step 5: Write `part-4/README.md` and commit**

```markdown
# part-4 — Core Trading Layer

Local-only web app: browse Polymarket markets (via part-3's synced Postgres data),
place real market-order buy/sell trades, view live balance/positions.

## Backend

    cd part-4/backend
    cp .env.example .env   # fill in DATABASE_URL (read-only role) + Polymarket creds
    pip install -r requirements.txt -r requirements-dev.txt
    uvicorn app.main:app --reload --port 8420

## Frontend

    cd part-4/frontend
    cp .env.example .env   # VITE_API_BASE_URL=http://localhost:8420
    npm install
    npm run dev             # http://localhost:5173

## Tests

    cd part-4/backend
    pytest                                                     # unit tests
    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest   # + DB tests

    cd part-4/frontend
    npm test
```

```bash
git add part-4/backend/app/main.py part-4/backend/tests/test_main_startup.py \
        part-4/backend/tests/conftest.py part-4/README.md
git commit -m "part-4: fail fast on missing config at startup, restrict CORS, add README"
```

---

### Task 10: Frontend scaffold, types, and API client

**Files:**
- Create: `part-4/frontend/package.json`
- Create: `part-4/frontend/tsconfig.json`
- Create: `part-4/frontend/vite.config.ts`
- Create: `part-4/frontend/index.html`
- Create: `part-4/frontend/.env.example`
- Create: `part-4/frontend/src/types.ts`
- Create: `part-4/frontend/src/api/client.ts`
- Create: `part-4/frontend/src/api/client.test.ts`

**Interfaces:**
- Produces: `MarketSummary`, `MarketDetail`, `Balance`, `Position`, `OrderRequest`,
  `OrderResult` TypeScript types (mirroring `app/schemas.py`); `listMarkets(params)`,
  `getMarket(id)`, `getBalance()`, `getPositions()`, `placeOrder(order)` — all typed
  fetch wrappers reading `VITE_API_BASE_URL`, used by every page in Tasks 11-13.

- [ ] **Step 1: Write the failing test**

```typescript
// part-4/frontend/src/api/client.test.ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { getMarket, placeOrder } from "./client";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

describe("getMarket", () => {
  it("fetches and parses a market by id", async () => {
    (fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ id: "1", question: "Q?", live_yes_price: 0.6 }),
    });

    const market = await getMarket("1");

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/markets/1"),
      expect.anything()
    );
    expect(market.live_yes_price).toBe(0.6);
  });
});

describe("placeOrder", () => {
  it("throws with the backend's error detail on a non-2xx response", async () => {
    (fetch as any).mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: "slippage exceeded" }),
    });

    await expect(
      placeOrder({ market_id: "1", outcome: "YES", side: "BUY", usdc_size: 5 })
    ).rejects.toThrow("slippage exceeded");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/frontend && npm test`
Expected: FAIL — `src/api/client.ts` doesn't exist yet (and no test runner is
configured yet either).

- [ ] **Step 3: Write scaffold files, types, and the client**

```json
// part-4/frontend/package.json
{
  "name": "part-4-frontend",
  "private": true,
  "version": "0.0.1",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "vitest run"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "react-router-dom": "^6.26.0"
  },
  "devDependencies": {
    "@testing-library/react": "^16.0.0",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "jsdom": "^25.0.0",
    "typescript": "^5.6.0",
    "vite": "^5.4.0",
    "vitest": "^2.1.0"
  }
}
```

```json
// part-4/frontend/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["ES2020", "DOM"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "skipLibCheck": true,
    "esModuleInterop": true
  },
  "include": ["src"]
}
```

```typescript
// part-4/frontend/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
  },
});
```

```html
<!-- part-4/frontend/index.html -->
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>part-4 trading app</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

```
# part-4/frontend/.env.example
VITE_API_BASE_URL=http://localhost:8420
```

```typescript
// part-4/frontend/src/types.ts
export type Side = "BUY" | "SELL";
export type Outcome = "YES" | "NO";

export interface MarketSummary {
  id: string;
  question: string;
  slug: string | null;
  end_date: string | null;
  current_score: number | null;
  volume_24h: number | null;
  liquidity: number | null;
}

export interface MarketDetail extends MarketSummary {
  description: string;
  live_yes_price: number;
}

export interface OrderRequest {
  market_id: string;
  outcome: Outcome;
  side: Side;
  usdc_size: number;
}

export interface OrderResult {
  order_id: string;
  market_id: string;
  outcome: Outcome;
  side: Side;
  executed_price: number;
  usdc_size: number;
}

export interface Position {
  market_id: string;
  outcome: Outcome;
  size: number;
  average_price: number;
  current_value: number;
}

export interface Balance {
  usdc_available: number;
}
```

```typescript
// part-4/frontend/src/api/client.ts
import type {
  Balance,
  MarketDetail,
  MarketSummary,
  OrderRequest,
  OrderResult,
  Position,
} from "../types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8420";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail ?? `request failed with status ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export function listMarkets(params: { minPrice?: number; maxPrice?: number } = {}) {
  const query = new URLSearchParams();
  if (params.minPrice !== undefined) query.set("min_price", String(params.minPrice));
  if (params.maxPrice !== undefined) query.set("max_price", String(params.maxPrice));
  return request<MarketSummary[]>(`/markets?${query.toString()}`);
}

export function getMarket(id: string) {
  return request<MarketDetail>(`/markets/${id}`);
}

export function getBalance() {
  return request<Balance>("/account/balance");
}

export function getPositions() {
  return request<Position[]>("/account/positions");
}

export function placeOrder(order: OrderRequest) {
  return request<OrderResult>("/orders", {
    method: "POST",
    body: JSON.stringify(order),
  });
}
```

- [ ] **Step 4: Install dependencies and run the test to verify it passes**

Run: `cd part-4/frontend && npm install && npm test`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/frontend/package.json part-4/frontend/tsconfig.json \
        part-4/frontend/vite.config.ts part-4/frontend/index.html \
        part-4/frontend/.env.example part-4/frontend/src/types.ts \
        part-4/frontend/src/api/client.ts part-4/frontend/src/api/client.test.ts
git commit -m "part-4: add frontend scaffold, shared types, and typed API client"
```

---

### Task 11: `ConfirmOrderDialog` component

**Files:**
- Create: `part-4/frontend/src/components/ConfirmOrderDialog.tsx`
- Create: `part-4/frontend/src/components/ConfirmOrderDialog.test.tsx`

**Interfaces:**
- Consumes: `MarketDetail`, `Side`, `Outcome` from `../types`.
- Produces: `ConfirmOrderDialog` React component, props `{ market: MarketDetail,
  outcome: Outcome, side: Side, usdcSize: number, onConfirm: () => void, onCancel:
  () => void }` — shows the live price and estimated cost/proceeds, requires an
  explicit confirm click. Used by `MarketDetail` page in Task 12.

- [ ] **Step 1: Write the failing test**

```typescript
// part-4/frontend/src/components/ConfirmOrderDialog.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConfirmOrderDialog } from "./ConfirmOrderDialog";
import type { MarketDetail } from "../types";

const market: MarketDetail = {
  id: "1",
  question: "Will X happen?",
  description: "",
  slug: "x",
  end_date: null,
  current_score: 0.6,
  volume_24h: 1000,
  liquidity: 2000,
  live_yes_price: 0.6,
};

describe("ConfirmOrderDialog", () => {
  it("shows the live price and estimated cost, and only fires onConfirm on click", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmOrderDialog
        market={market}
        outcome="YES"
        side="BUY"
        usdcSize={10}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />
    );

    expect(screen.getByText(/0.60/)).toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("fires onCancel and not onConfirm when cancelled", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmOrderDialog
        market={market}
        outcome="YES"
        side="BUY"
        usdcSize={10}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/frontend && npm test`
Expected: FAIL — `ConfirmOrderDialog` module doesn't exist; also add
`@testing-library/jest-dom` matchers setup if `toBeInTheDocument` is undefined.

- [ ] **Step 3: Add jest-dom setup and write the component**

```
# part-4/frontend/package.json  (add to devDependencies)
"@testing-library/jest-dom": "^6.5.0"
```

```typescript
// part-4/frontend/vite.config.ts  (modify test block)
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
  },
});
```

```typescript
// part-4/frontend/src/setupTests.ts
import "@testing-library/jest-dom/vitest";
```

```typescript
// part-4/frontend/src/components/ConfirmOrderDialog.tsx
import type { MarketDetail, Outcome, Side } from "../types";

interface Props {
  market: MarketDetail;
  outcome: Outcome;
  side: Side;
  usdcSize: number;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmOrderDialog({
  market,
  outcome,
  side,
  usdcSize,
  onConfirm,
  onCancel,
}: Props) {
  const price = outcome === "YES" ? market.live_yes_price : 1 - market.live_yes_price;
  const estimatedShares = usdcSize / price;

  return (
    <div role="dialog" aria-label="confirm order">
      <p>{market.question}</p>
      <p>
        {side} {outcome} at {price.toFixed(2)} — ${usdcSize.toFixed(2)} for an
        estimated {estimatedShares.toFixed(2)} shares
      </p>
      <button onClick={onCancel}>Cancel</button>
      <button onClick={onConfirm}>Confirm</button>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/frontend && npm install && npm test`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/frontend/package.json part-4/frontend/vite.config.ts \
        part-4/frontend/src/setupTests.ts \
        part-4/frontend/src/components/ConfirmOrderDialog.tsx \
        part-4/frontend/src/components/ConfirmOrderDialog.test.tsx
git commit -m "part-4: add ConfirmOrderDialog with explicit confirm/cancel"
```

---

### Task 12: `MarketList` and `MarketDetail` pages

**Files:**
- Create: `part-4/frontend/src/pages/MarketList.tsx`
- Create: `part-4/frontend/src/pages/MarketList.test.tsx`
- Create: `part-4/frontend/src/pages/MarketDetail.tsx`
- Create: `part-4/frontend/src/pages/MarketDetail.test.tsx`

**Interfaces:**
- Consumes: `listMarkets`, `getMarket`, `placeOrder` from `../api/client`;
  `ConfirmOrderDialog` from Task 11.
- Produces: `MarketList` (renders markets, links to detail by id),
  `MarketDetail` (shows live price, Buy YES / Buy NO / Sell buttons, wires the
  confirm dialog before calling `placeOrder`, shows the `OrderResult` or error).
  Both are default-exported for use in `App.tsx` (Task 13).

- [ ] **Step 1: Write the failing tests**

```typescript
// part-4/frontend/src/pages/MarketList.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { MarketList } from "./MarketList";

vi.mock("../api/client");

describe("MarketList", () => {
  it("renders markets returned by the API", async () => {
    vi.mocked(api.listMarkets).mockResolvedValue([
      {
        id: "1", question: "Will X happen?", slug: "x", end_date: null,
        current_score: 0.6, volume_24h: 1000, liquidity: 2000,
      },
    ]);

    render(<MemoryRouter><MarketList /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText("Will X happen?")).toBeInTheDocument());
  });
});
```

```typescript
// part-4/frontend/src/pages/MarketDetail.test.tsx
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { MarketDetail } from "./MarketDetail";

vi.mock("../api/client");

function renderAtMarket(id: string) {
  return render(
    <MemoryRouter initialEntries={[`/markets/${id}`]}>
      <Routes>
        <Route path="/markets/:id" element={<MarketDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("MarketDetail", () => {
  it("places an order only after the confirm dialog is accepted", async () => {
    vi.mocked(api.getMarket).mockResolvedValue({
      id: "1", question: "Will X happen?", description: "d", slug: "x",
      end_date: null, current_score: 0.6, volume_24h: 1000, liquidity: 2000,
      live_yes_price: 0.6,
    });
    vi.mocked(api.placeOrder).mockResolvedValue({
      order_id: "o1", market_id: "1", outcome: "YES", side: "BUY",
      executed_price: 0.6, usdc_size: 5,
    });

    renderAtMarket("1");
    await waitFor(() => expect(screen.getByText("Will X happen?")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /buy yes/i }));
    expect(api.placeOrder).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
    await waitFor(() => expect(api.placeOrder).toHaveBeenCalledWith(
      expect.objectContaining({ market_id: "1", outcome: "YES", side: "BUY" })
    ));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/frontend && npm install react-router-dom && npm test`
Expected: FAIL — `MarketList`/`MarketDetail` modules don't exist yet.

- [ ] **Step 3: Write the two pages**

```typescript
// part-4/frontend/src/pages/MarketList.tsx
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listMarkets } from "../api/client";
import type { MarketSummary } from "../types";

export function MarketList() {
  const [markets, setMarkets] = useState<MarketSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listMarkets().then(setMarkets).catch((e) => setError(e.message));
  }, []);

  if (error) return <p role="alert">{error}</p>;

  return (
    <ul>
      {markets.map((m) => (
        <li key={m.id}>
          <Link to={`/markets/${m.id}`}>{m.question}</Link>
        </li>
      ))}
    </ul>
  );
}
```

```typescript
// part-4/frontend/src/pages/MarketDetail.tsx
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getMarket, placeOrder } from "../api/client";
import { ConfirmOrderDialog } from "../components/ConfirmOrderDialog";
import type { MarketDetail as MarketDetailType, Outcome, Side } from "../types";

const USDC_SIZE = 5.0; // v1: fixed manual trade size, matches spec's simplicity goal

export function MarketDetail() {
  const { id } = useParams<{ id: string }>();
  const [market, setMarket] = useState<MarketDetailType | null>(null);
  const [pending, setPending] = useState<{ outcome: Outcome; side: Side } | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (id) getMarket(id).then(setMarket).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <p role="alert">{error}</p>;
  if (!market) return <p>Loading...</p>;

  async function confirm() {
    if (!pending || !market) return;
    try {
      const order = await placeOrder({
        market_id: market.id,
        outcome: pending.outcome,
        side: pending.side,
        usdc_size: USDC_SIZE,
      });
      setResult(`Filled ${order.usdc_size} @ ${order.executed_price}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(null);
    }
  }

  return (
    <div>
      <h1>{market.question}</h1>
      <p>Live YES price: {market.live_yes_price.toFixed(2)}</p>
      <button onClick={() => setPending({ outcome: "YES", side: "BUY" })}>
        Buy YES
      </button>
      <button onClick={() => setPending({ outcome: "NO", side: "BUY" })}>
        Buy NO
      </button>
      <button onClick={() => setPending({ outcome: "YES", side: "SELL" })}>
        Sell YES
      </button>
      {pending && (
        <ConfirmOrderDialog
          market={market}
          outcome={pending.outcome}
          side={pending.side}
          usdcSize={USDC_SIZE}
          onConfirm={confirm}
          onCancel={() => setPending(null)}
        />
      )}
      {result && <p>{result}</p>}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-4/frontend && npm test`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add part-4/frontend/package.json part-4/frontend/src/pages/MarketList.tsx \
        part-4/frontend/src/pages/MarketList.test.tsx \
        part-4/frontend/src/pages/MarketDetail.tsx \
        part-4/frontend/src/pages/MarketDetail.test.tsx
git commit -m "part-4: add MarketList and MarketDetail pages with confirm-gated trading"
```

---

### Task 13: `Account` page, `App.tsx` routing, and entrypoint

**Files:**
- Create: `part-4/frontend/src/pages/Account.tsx`
- Create: `part-4/frontend/src/pages/Account.test.tsx`
- Create: `part-4/frontend/src/App.tsx`
- Create: `part-4/frontend/src/main.tsx`

**Interfaces:**
- Consumes: `getBalance`, `getPositions` from `../api/client`; `MarketList`,
  `MarketDetail` from Task 12.
- Produces: complete, runnable frontend — `App.tsx` wires `react-router-dom` routes
  `/` (MarketList), `/markets/:id` (MarketDetail), `/account` (Account).

- [ ] **Step 1: Write the failing test**

```typescript
// part-4/frontend/src/pages/Account.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { Account } from "./Account";

vi.mock("../api/client");

describe("Account", () => {
  it("renders balance and positions from the API", async () => {
    vi.mocked(api.getBalance).mockResolvedValue({ usdc_available: 42.5 });
    vi.mocked(api.getPositions).mockResolvedValue([
      { market_id: "1", outcome: "YES", size: 10, average_price: 0.5, current_value: 6 },
    ]);

    render(<Account />);

    await waitFor(() => expect(screen.getByText(/42.5/)).toBeInTheDocument());
    expect(screen.getByText(/1/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-4/frontend && npm test`
Expected: FAIL — `Account` module doesn't exist yet.

- [ ] **Step 3: Write `Account.tsx`, `App.tsx`, and `main.tsx`**

```typescript
// part-4/frontend/src/pages/Account.tsx
import { useEffect, useState } from "react";
import { getBalance, getPositions } from "../api/client";
import type { Balance, Position } from "../types";

export function Account() {
  const [balance, setBalance] = useState<Balance | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);

  useEffect(() => {
    getBalance().then(setBalance);
    getPositions().then(setPositions);
  }, []);

  return (
    <div>
      <h1>Account</h1>
      {balance && <p>Balance: ${balance.usdc_available.toFixed(2)}</p>}
      <ul>
        {positions.map((p) => (
          <li key={`${p.market_id}-${p.outcome}`}>
            {p.market_id} {p.outcome}: {p.size} shares @ {p.average_price} (worth $
            {p.current_value.toFixed(2)})
          </li>
        ))}
      </ul>
    </div>
  );
}
```

```typescript
// part-4/frontend/src/App.tsx
import { Link, Route, Routes } from "react-router-dom";
import { Account } from "./pages/Account";
import { MarketDetail } from "./pages/MarketDetail";
import { MarketList } from "./pages/MarketList";

export function App() {
  return (
    <div>
      <nav>
        <Link to="/">Markets</Link> | <Link to="/account">Account</Link>
      </nav>
      <Routes>
        <Route path="/" element={<MarketList />} />
        <Route path="/markets/:id" element={<MarketDetail />} />
        <Route path="/account" element={<Account />} />
      </Routes>
    </div>
  );
}
```

```typescript
// part-4/frontend/src/main.tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
```

- [ ] **Step 4: Run all frontend tests to verify everything passes**

Run: `cd part-4/frontend && npm test`
Expected: PASS on all test files (client, ConfirmOrderDialog, MarketList,
MarketDetail, Account).

- [ ] **Step 5: Commit**

```bash
git add part-4/frontend/src/pages/Account.tsx part-4/frontend/src/pages/Account.test.tsx \
        part-4/frontend/src/App.tsx part-4/frontend/src/main.tsx
git commit -m "part-4: add Account page and wire up app routing/entrypoint"
```

---

### Task 14: Manual end-to-end smoke test (real money, documented not automated)

**Files:**
- Modify: `part-4/README.md`

This is the one step that necessarily touches real funds — it is a documented
manual procedure, not an automated test, per the spec's testing section.

- [ ] **Step 1: Add the manual smoke test procedure to the README**

```markdown
## Manual smoke test (real money — run by hand, not in CI)

Before relying on this app for anything larger, verify the full path once with a
small real order:

1. Start the backend (`uvicorn app.main:app --port 8420`) and frontend (`npm run
   dev`) per the instructions above, pointed at your real `.env` credentials.
2. Open the app, pick a liquid market (high `volume_24h`), and note its current
   live YES price shown on the detail page.
3. Click "Buy YES", confirm in the dialog, and submit a $1 order.
4. Confirm the response shows a filled order id and an executed price close to the
   quoted one (within `MAX_SLIPPAGE_PCT`).
5. Go to the Account page and confirm:
   - `usdc_available` dropped by approximately $1 (minus/plus the fill price
     difference).
   - A new position appears for that market/outcome with the expected size.
6. Optionally repeat with a "Sell" to confirm the round trip, and check the
   position disappears or shrinks accordingly.

If any step disagrees with what Polymarket's own UI shows for the same wallet,
stop and investigate before placing further real orders.
```

- [ ] **Step 2: Commit**

```bash
git add part-4/README.md
git commit -m "part-4: document the manual real-money smoke test procedure"
```

---

## Self-Review Notes

- **Spec coverage:** architecture (Task 9's CORS/startup + overall file structure),
  backend components (Tasks 1-9), frontend components (Tasks 10-13), data flow /
  confirm-before-order (Tasks 11-12), error handling — order failures/slippage
  (Task 6), DB-unavailable isolation (markets routes vs trading routes use
  independent dependencies, Tasks 7-8), fail-fast key config (Task 1, Task 9),
  testing plan (unit tests throughout, DB tests skip without `TEST_DATABASE_URL`,
  manual real-money smoke test — Task 14) are all covered by a task each.
- **Type consistency:** `OrderRequest`/`OrderResult`/`Balance`/`Position` defined in
  Task 2 (`app/schemas.py`) are consumed unchanged by Tasks 4-8; their TypeScript
  mirrors in Task 10 (`types.ts`) use the same field names so `client.ts`'s JSON
  round-trip needs no field mapping.
- **No placeholders:** the one intentionally-approximate value is `MarketsRepo.
  get_market`'s `live_yes_price` (Task 3), explicitly called out as overwritten by
  the route layer in Task 7 — not a TODO, a documented intermediate value.
