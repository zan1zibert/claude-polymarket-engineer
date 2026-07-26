"""Unit tests for the Gamma payload parsers and status fetcher in lib/polymarket.

Pure functions, no network — these pin down the two price parsers the syncer
relies on: `current_yes_price` (live price for the price series) and
`resolved_yes_price` (settled 0/1 outcome). Gamma is inconsistent about whether
list fields arrive as JSON lists or JSON-encoded strings, so both forms are
covered.

`fetch_statuses` does hit the network (mocked via respx here) — it's the one
place the `closed` query param matters: Gamma's `/markets?id=...` lookup
implicitly filters to `closed=false` when the param is omitted, so a market
that has already resolved is silently absent from the response unless
`closed=True` is passed explicitly. That's exactly the bug that let the
"awaiting outcome" backfill never fire.
"""
import httpx
import respx

from lib.polymarket import GAMMA_MARKETS_URL, current_yes_price, fetch_statuses, resolved_yes_price


class TestCurrentYesPrice:
    def test_parses_json_string_fields(self):
        # Gamma often returns these list fields as JSON-encoded strings.
        m = {"outcomes": '["Yes","No"]', "outcomePrices": '["0.37","0.63"]'}
        assert current_yes_price(m) == 0.37

    def test_parses_native_list_fields(self):
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.9", "0.1"]}
        assert current_yes_price(m) == 0.9

    def test_outcome_order_independent(self):
        # YES leg is picked by name, not position.
        m = {"outcomes": ["No", "Yes"], "outcomePrices": ["0.2", "0.8"]}
        assert current_yes_price(m) == 0.8

    def test_returns_raw_mid_price_not_rounded_to_outcome(self):
        # Unlike resolved_yes_price, a mid-range price is returned as-is.
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.51", "0.49"]}
        assert current_yes_price(m) == 0.51

    def test_rounds_to_four_dp(self):
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.123456", "0.876544"]}
        assert current_yes_price(m) == 0.1235

    def test_non_binary_returns_none(self):
        m = {"outcomes": ["A", "B", "C"], "outcomePrices": ["0.3", "0.3", "0.4"]}
        assert current_yes_price(m) is None

    def test_missing_prices_returns_none(self):
        assert current_yes_price({"outcomes": ["Yes", "No"]}) is None

    def test_missing_yes_leg_returns_none(self):
        m = {"outcomes": ["Up", "Down"], "outcomePrices": ["0.6", "0.4"]}
        assert current_yes_price(m) is None

    def test_non_numeric_price_returns_none(self):
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["high", "low"]}
        assert current_yes_price(m) is None

    def test_empty_payload_returns_none(self):
        assert current_yes_price({}) is None


class TestResolvedYesPrice:
    """Sanity checks — resolved_yes_price shares the parsing path but gates to a
    definitive 0/1, which is exactly how it must differ from current_yes_price."""

    def test_settled_yes_won(self):
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["1", "0"]}
        assert resolved_yes_price(m) == 1.0

    def test_settled_no_won(self):
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["0", "1"]}
        assert resolved_yes_price(m) == 0.0

    def test_open_midrange_is_not_settled(self):
        # A live 0.37 is a valid current price but NOT a resolved outcome.
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.37", "0.63"]}
        assert resolved_yes_price(m) is None
        assert current_yes_price(m) == 0.37

    def test_settled_fifty_fifty_is_not_rounded_to_no(self):
        # A canceled/tied event settles 50/50 — must not collapse to 0.0 via
        # round-half-to-even (round(0.5) == 0 in Python).
        m = {"outcomes": ["Yes", "No"], "outcomePrices": ["0.5", "0.5"]}
        assert resolved_yes_price(m) == 0.5


class TestFetchStatuses:
    """`closed` must reach Gamma as an explicit query param, not be omitted —
    omitting it silently drops closed markets from the response."""

    @respx.mock
    def test_default_requests_open_markets(self):
        route = respx.get(GAMMA_MARKETS_URL).mock(
            return_value=httpx.Response(200, json=[
                {
                    "id": "1", "closed": False, "endDate": "2026-01-01",
                    "outcomes": ["Yes", "No"], "outcomePrices": ["0.6", "0.4"],
                }
            ])
        )
        with httpx.Client() as client:
            result = fetch_statuses(client, ["1"])

        assert route.calls.last.request.url.params["closed"] == "false"
        assert result["1"]["yes_price"] == 0.6

    @respx.mock
    def test_closed_true_requests_resolved_markets(self):
        route = respx.get(GAMMA_MARKETS_URL).mock(
            return_value=httpx.Response(200, json=[
                {
                    "id": "2", "closed": True, "endDate": "2026-01-01",
                    "outcomes": ["Yes", "No"], "outcomePrices": ["0", "1"],
                }
            ])
        )
        with httpx.Client() as client:
            result = fetch_statuses(client, ["2"], closed=True)

        assert route.calls.last.request.url.params["closed"] == "true"
        assert result["2"]["resolved_outcome"] == 0.0

    @respx.mock
    def test_id_missing_from_response_is_absent_from_result(self):
        # Gamma's implicit closed=false default means a resolved id queried
        # without closed=True comes back empty — the exact failure mode that
        # left the awaiting-outcome backfill permanently stuck.
        respx.get(GAMMA_MARKETS_URL).mock(return_value=httpx.Response(200, json=[]))

        with httpx.Client() as client:
            result = fetch_statuses(client, ["3"])

        assert result == {}
