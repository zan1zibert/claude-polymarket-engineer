"""Unit tests for the Gamma payload parsers in lib/polymarket.

Pure functions, no network — these pin down the two price parsers the syncer
relies on: `current_yes_price` (live price for the price series) and
`resolved_yes_price` (settled 0/1 outcome). Gamma is inconsistent about whether
list fields arrive as JSON lists or JSON-encoded strings, so both forms are
covered.
"""
from lib.polymarket import current_yes_price, resolved_yes_price


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
