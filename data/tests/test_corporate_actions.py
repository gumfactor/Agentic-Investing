"""Tests for corporate action adjustment factor computation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from data.normalization.corporate_actions import compute_adjustment_factors, apply_adjustment_factors


def _prices(rows: list[tuple]) -> pd.DataFrame:
    """Helper: list of (ticker, date_str, close) tuples -> DataFrame."""
    return pd.DataFrame(
        [{"ticker": t, "date": date.fromisoformat(d), "close": Decimal(str(c))} for t, d, c in rows]
    )


def _actions(rows: list[tuple]) -> pd.DataFrame:
    """Helper: list of (ticker, ex_date_str, action_type, value) tuples -> DataFrame."""
    return pd.DataFrame(
        [
            {
                "ticker": t,
                "ex_date": date.fromisoformat(d),
                "action_type": at,
                "value": Decimal(str(v)),
            }
            for t, d, at, v in rows
        ]
    )


class TestSplitAdjustment:
    def test_2_for_1_split_halves_prior_prices(self) -> None:
        prices = _prices([
            ("AAPL", "2024-01-01", 200),
            ("AAPL", "2024-01-02", 200),   # ex-date: after this day, factor = 1
            ("AAPL", "2024-01-03", 100),   # post-split
        ])
        actions = _actions([("AAPL", "2024-01-03", "split", 2.0)])

        factors = compute_adjustment_factors(actions, prices)
        factor_map = dict(zip(factors["date"], factors["adj_factor"]))

        # Dates before ex-date should be divided by 2
        assert factor_map[date(2024, 1, 1)] == Decimal("0.5")
        assert factor_map[date(2024, 1, 2)] == Decimal("0.5")
        # Post-split date: no adjustment (factor = 1)
        assert factor_map[date(2024, 1, 3)] == Decimal("1")

    def test_reverse_split_multiplies_prior_prices(self) -> None:
        prices = _prices([
            ("TEST", "2024-01-01", 5),
            ("TEST", "2024-01-02", 15),   # ex-date of 1-for-3 reverse split
        ])
        # 1-for-3 reverse split: value = 1/3 (new_shares / old_shares)
        actions = _actions([("TEST", "2024-01-02", "split", Decimal("0.33333333"))])

        factors = compute_adjustment_factors(actions, prices)
        factor_map = dict(zip(factors["date"], factors["adj_factor"]))

        # Before split, price needs to be multiplied (adjusted up to match post-split scale)
        assert factor_map[date(2024, 1, 1)] > Decimal("1")

    def test_no_actions_factor_is_one(self) -> None:
        prices = _prices([("MSFT", "2024-01-01", 400), ("MSFT", "2024-01-02", 401)])
        factors = compute_adjustment_factors(pd.DataFrame(), prices)
        assert all(factors["adj_factor"] == Decimal("1"))

    def test_empty_prices_returns_empty(self) -> None:
        result = compute_adjustment_factors(pd.DataFrame(), pd.DataFrame())
        assert result.empty


class TestDividendAdjustment:
    def test_dividend_reduces_prior_prices(self) -> None:
        # $1 dividend on a $100 stock -> factor = 99/100 = 0.99 for prior dates
        prices = _prices([
            ("DIV", "2024-01-01", 100),
            ("DIV", "2024-01-02", 100),   # ex-date
            ("DIV", "2024-01-03", 99),
        ])
        actions = _actions([("DIV", "2024-01-02", "dividend", 1.0)])

        factors = compute_adjustment_factors(actions, prices)
        factor_map = dict(zip(factors["date"], factors["adj_factor"]))

        # Factor for Jan 1 (before ex-date Jan 2) = 99/100 = 0.99
        assert factor_map[date(2024, 1, 1)] == Decimal("0.99")
        # On and after ex-date: factor = 1
        assert factor_map[date(2024, 1, 2)] == Decimal("1")

    def test_zero_dividend_value_skipped(self) -> None:
        prices = _prices([("X", "2024-01-01", 50), ("X", "2024-01-02", 50)])
        actions = _actions([("X", "2024-01-02", "dividend", 0.0)])
        # Zero dividend: no factor change expected
        factors = compute_adjustment_factors(actions, prices)
        factor_map = dict(zip(factors["date"], factors["adj_factor"]))
        # Both dates should have factor = 1 (zero dividend is a no-op)
        assert all(v == Decimal("1") for v in factor_map.values())


class TestApplyAdjustmentFactors:
    def test_applies_factor_to_ohlc(self) -> None:
        prices = _prices([("AAPL", "2024-01-01", 200)])
        prices["open"] = Decimal("198")
        prices["high"] = Decimal("205")
        prices["low"] = Decimal("197")

        factors = pd.DataFrame([
            {"ticker": "AAPL", "date": date(2024, 1, 1), "adj_factor": Decimal("0.5")}
        ])

        result = apply_adjustment_factors(prices, factors)

        assert result.iloc[0]["adj_close"] == Decimal("100.000000")
        assert result.iloc[0]["adj_open"] == Decimal("99.000000")
        assert result.iloc[0]["adj_high"] == Decimal("102.500000")
        assert result.iloc[0]["adj_low"] == Decimal("98.500000")

    def test_missing_factor_defaults_to_one(self) -> None:
        prices = _prices([("AAPL", "2024-01-01", 200)])
        factors = pd.DataFrame(columns=["ticker", "date", "adj_factor"])

        result = apply_adjustment_factors(prices, factors)
        assert result.iloc[0]["adj_close"] == Decimal("200")
