"""Tests for corporate action adjustment factor computation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from data.normalization.corporate_actions import (
    apply_adjustment_factors,
    build_realized_total_return_as_of,
    build_score_price_history_as_of,
    compute_adjustment_factors,
)


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


# ─── Cutoff-aware as-of builders (BUG-009 section 2.3) ───────────────────────

def _actions_with_known_at(rows: list[tuple]) -> pd.DataFrame:
    """Helper: (ticker, ex_date_str, action_type, value, known_at, source_version)."""
    return pd.DataFrame(
        [
            {
                "ticker": t,
                "ex_date": date.fromisoformat(d),
                "action_type": at,
                "value": Decimal(str(v)),
                "known_at": known_at,
                "source_version": sv,
            }
            for t, d, at, v, known_at, sv in rows
        ]
    )


class TestBuildScorePriceHistoryAsOf:
    def test_action_known_before_cutoff_is_applied(self) -> None:
        prices = _prices([
            ("AAPL", "2024-01-01", 200),
            ("AAPL", "2024-01-02", 200),
            ("AAPL", "2024-01-03", 100),
        ])
        actions = _actions_with_known_at([
            ("AAPL", "2024-01-03", "split", 2.0,
             datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc), "v1"),
        ])
        cutoff = datetime(2024, 1, 3, 21, 0, tzinfo=timezone.utc)
        adjusted, metadata = build_score_price_history_as_of(prices, actions, cutoff)
        row = adjusted[adjusted["date"] == date(2024, 1, 1)].iloc[0]
        assert row["adj_close"] == Decimal("100.000000")
        assert metadata.n_actions_excluded_by_cutoff == 0
        assert metadata.action_source_versions == ("v1",)

    def test_action_announced_after_cutoff_excluded_even_with_earlier_effective_date(self) -> None:
        """§2.5 acceptance test: an action announced after the score cutoff must be
        excluded from score inputs even if its effective (ex) date is earlier."""
        prices = _prices([
            ("AAPL", "2024-01-01", 200),
            ("AAPL", "2024-01-02", 200),
            ("AAPL", "2024-01-03", 100),
        ])
        # The split's ex_date (2024-01-03) is at/after the cutoff, but even a
        # hypothetical earlier ex_date wouldn't matter: known_at (2024-01-05,
        # AFTER cutoff) is what gates inclusion, not ex_date.
        actions = _actions_with_known_at([
            ("AAPL", "2024-01-03", "split", 2.0,
             datetime(2024, 1, 5, 21, 0, tzinfo=timezone.utc), "v1"),
        ])
        cutoff = datetime(2024, 1, 3, 21, 0, tzinfo=timezone.utc)
        adjusted, metadata = build_score_price_history_as_of(prices, actions, cutoff)
        row = adjusted[adjusted["date"] == date(2024, 1, 1)].iloc[0]
        # Not adjusted: action was not knowable by the score cutoff.
        assert row["adj_close"] == Decimal("200.000000")
        assert metadata.n_actions_excluded_by_cutoff == 1

    def test_future_split_leaves_score_feature_numerically_identical(self) -> None:
        """§2.5 acceptance test: adding a future split must not change score[t]."""
        prices = _prices([
            ("AAPL", "2024-01-01", 200),
            ("AAPL", "2024-01-02", 200),
        ])
        cutoff = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
        no_action_adjusted, _ = build_score_price_history_as_of(
            prices, pd.DataFrame(columns=["ticker", "ex_date", "action_type", "value", "known_at", "source_version"]), cutoff
        )
        future_actions = _actions_with_known_at([
            ("AAPL", "2024-06-01", "split", 2.0,
             datetime(2024, 6, 1, 21, 0, tzinfo=timezone.utc), "v1"),
        ])
        with_future_action_adjusted, _ = build_score_price_history_as_of(prices, future_actions, cutoff)

        before = no_action_adjusted[no_action_adjusted["date"] == date(2024, 1, 1)].iloc[0]["adj_close"]
        after = with_future_action_adjusted[with_future_action_adjusted["date"] == date(2024, 1, 1)].iloc[0]["adj_close"]
        assert before == after == Decimal("200.000000")

    def test_missing_known_at_column_raises(self) -> None:
        prices = _prices([("AAPL", "2024-01-01", 200)])
        actions = _actions([("AAPL", "2024-01-02", "split", 2.0)])  # no known_at column
        with pytest.raises(ValueError, match="known_at"):
            build_score_price_history_as_of(prices, actions, datetime(2024, 1, 2, tzinfo=timezone.utc))

    def test_null_known_at_row_excluded_not_included(self) -> None:
        prices = _prices([
            ("AAPL", "2024-01-01", 200),
            ("AAPL", "2024-01-02", 100),
        ])
        actions = _actions_with_known_at([
            ("AAPL", "2024-01-02", "split", 2.0, None, "v1"),
        ])
        cutoff = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
        adjusted, metadata = build_score_price_history_as_of(prices, actions, cutoff)
        row = adjusted[adjusted["date"] == date(2024, 1, 1)].iloc[0]
        assert row["adj_close"] == Decimal("200.000000")  # not adjusted
        assert metadata.n_actions_excluded_missing_known_at == 1


class TestBuildRealizedTotalReturnAsOf:
    def test_action_known_by_exit_cutoff_included_with_source_version(self) -> None:
        prices = _prices([
            ("AAPL", "2024-01-02", 200),
            ("AAPL", "2024-01-03", 200),
            ("AAPL", "2024-01-08", 100),
        ])
        actions = _actions_with_known_at([
            ("AAPL", "2024-01-08", "split", 2.0,
             datetime(2024, 1, 5, 21, 0, tzinfo=timezone.utc), "v2"),
        ])
        exit_cutoff = datetime(2024, 1, 8, 21, 0, tzinfo=timezone.utc)
        adjusted, metadata = build_realized_total_return_as_of(
            prices, actions, entry_date=date(2024, 1, 2), exit_cutoff=exit_cutoff
        )
        row = adjusted[adjusted["date"] == date(2024, 1, 2)].iloc[0]
        assert row["adj_close"] == Decimal("100.000000")
        assert metadata.action_source_versions == ("v2",)
        assert metadata.builder == "build_realized_total_return_as_of"


class TestSplitDividendPortfolioAccountingParity:
    """Design plan section 2.2 acceptance test: a buy-and-hold fixture
    spanning a split and a dividend must produce the SAME total return from
    (a) portfolio accounting math (shares/cash, computed analytically here —
    no execution code is touched) and (b) the analytic adjusted-return
    series from build_realized_total_return_as_of. Order/fill notional in
    (a) uses only raw (unadjusted) prices, matching section 2.2's
    requirement that fills never use adjusted prices.

    Fixture: buy 10 shares at raw close 100 on d0. A 2-for-1 split on d1
    (raw close 50, i.e. exactly half of d0's close: no independent price
    movement between d0 and d1, so the split is the only thing happening)
    doubles the share count to 20, cost basis conserved. A $1/share
    dividend on d2 (raw close 50) is reinvested — using the total-return
    convention build_realized_total_return_as_of/compute_adjustment_factors
    implements: divide the prior price by (ex_close - div) / ex_close,
    equivalently buy additional shares at the theoretical ex-dividend price
    (ex_close - div) — yielding shares_after = shares_before * ex_close /
    (ex_close - div) = 20 * 50 / 49 = 1000/49. Ending raw close on d3 is 60
    (a genuine +20% move from d2, independent of any corporate action).

    Hand-derived total return: (1000/49 * 60) / 1000 - 1 = 60/49 - 1
    = 0.22448979591836735 (11/49).
    """

    def test_portfolio_accounting_matches_analytic_adjusted_series(self) -> None:
        d0, d1, d2, d3 = (
            date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5),
        )
        raw_closes = {d0: Decimal("100"), d1: Decimal("50"), d2: Decimal("50"), d3: Decimal("60")}
        prices = pd.DataFrame(
            [{"ticker": "AAPL", "date": d, "close": c} for d, c in raw_closes.items()]
        )

        # ── (a) Portfolio accounting: raw prices only, shares + cash ──────────
        n0_shares = Decimal("10")
        raw_entry_price = raw_closes[d0]
        initial_notional = n0_shares * raw_entry_price  # order/fill notional: RAW price
        assert initial_notional == Decimal("1000")

        # Split: shares double, cost basis conserved (no cash flow).
        split_ratio = Decimal("2")
        shares_after_split = n0_shares * split_ratio
        assert shares_after_split == Decimal("20")

        # Dividend: $1/share, reinvested at the theoretical ex-dividend price
        # (ex_close - div) — the convention compute_adjustment_factors
        # implements via factor = (ex_close - div) / ex_close.
        div_per_share = Decimal("1")
        ex_close = raw_closes[d2]
        shares_after_dividend = shares_after_split * ex_close / (ex_close - div_per_share)

        # Exit: raw close on d3 — fill/order notional is raw, never adjusted.
        raw_exit_price = raw_closes[d3]
        ending_value = shares_after_dividend * raw_exit_price
        portfolio_total_return = ending_value / initial_notional - Decimal("1")

        # ── (b) Analytic adjusted-return series ────────────────────────────────
        actions = _actions_with_known_at([
            ("AAPL", str(d1), "split", 2.0,
             datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc), "v1"),
            ("AAPL", str(d2), "dividend", 1.0,
             datetime(2024, 1, 3, 21, 0, tzinfo=timezone.utc), "v1"),
        ])
        exit_cutoff = datetime(2024, 1, 5, 21, 0, tzinfo=timezone.utc)
        adjusted, metadata = build_realized_total_return_as_of(
            prices, actions, entry_date=d0, exit_cutoff=exit_cutoff
        )
        adj_start = adjusted[adjusted["date"] == d0].iloc[0]["adj_close"]
        adj_end = adjusted[adjusted["date"] == d3].iloc[0]["adj_close"]
        analytic_total_return = adj_end / adj_start - Decimal("1")

        # ── Parity ──────────────────────────────────────────────────────────
        assert abs(portfolio_total_return - analytic_total_return) < Decimal("0.000001")

        # Hand-derived exact value: 60/49 - 1.
        expected = Decimal("60") / Decimal("49") - Decimal("1")
        assert abs(portfolio_total_return - expected) < Decimal("0.000001")
        assert abs(analytic_total_return - expected) < Decimal("0.000001")

        # Raw prices were never touched by the adjustment — the raw `close`
        # column on the returned frame is untouched and still the raw fill
        # price a real order would use.
        assert adjusted[adjusted["date"] == d0].iloc[0]["close"] == raw_entry_price
        assert adjusted[adjusted["date"] == d3].iloc[0]["close"] == raw_exit_price
        assert metadata.builder == "build_realized_total_return_as_of"
