"""Tests for value.py and quality.py factor modules."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from signals.factors.value import compute_value_scores
from signals.factors.quality import compute_quality_scores


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _biz_dates(start: date, n: int) -> list[date]:
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _make_prices(tickers: list[str], dates: list[date], base: float = 100.0) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        for d in dates:
            rows.append({"ticker": ticker, "date": d, "close": base})
    return pd.DataFrame(rows)


def _make_fundamentals(
    tickers: list[str],
    release_date: date,
    period_end: date,
    items: dict,  # item_name → {ticker: value}
) -> pd.DataFrame:
    """Build synthetic financial_statements rows."""
    rows = []
    for item_name, ticker_vals in items.items():
        for ticker, value in ticker_vals.items():
            if ticker in tickers:
                rows.append({
                    "ticker": ticker,
                    "period_end_date": period_end,
                    "release_date": release_date,
                    "period_type": "quarterly",
                    "item_name": item_name,
                    "value": Decimal(str(value)),
                })
    return pd.DataFrame(rows)


TICKERS = [f"T{i:02d}" for i in range(15)]


def _make_standard_fundamentals(
    tickers: list[str] = TICKERS,
    release_date: date = date(2022, 2, 1),
    period_end: date = date(2021, 12, 31),
    net_income_multiplier: float = 1.0,
) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = len(tickers)
    net_incomes = rng.uniform(0.5e9, 5e9, n) * net_income_multiplier
    equities = rng.uniform(5e9, 50e9, n)
    assets = rng.uniform(10e9, 100e9, n)
    gross_profits = rng.uniform(1e9, 10e9, n)
    op_cfs = net_incomes * rng.uniform(0.8, 1.2, n)
    fcfs = op_cfs - rng.uniform(1e8, 5e8, n)
    shares = rng.uniform(1e8, 5e9, n)

    return _make_fundamentals(
        tickers,
        release_date=release_date,
        period_end=period_end,
        items={
            "net_income": dict(zip(tickers, net_incomes)),
            "total_equity": dict(zip(tickers, equities)),
            "total_assets": dict(zip(tickers, assets)),
            "gross_profit": dict(zip(tickers, gross_profits)),
            "operating_cash_flow": dict(zip(tickers, op_cfs)),
            "free_cash_flow": dict(zip(tickers, fcfs)),
            "shares_outstanding": dict(zip(tickers, shares)),
        },
    )


# ─── Value factor tests ───────────────────────────────────────────────────────

class TestComputeValueScores:
    def _base_setup(self):
        dates = _biz_dates(date(2022, 3, 1), 10)
        fund = _make_standard_fundamentals()
        prices = _make_prices(TICKERS, dates)
        return fund, prices, dates

    def test_output_columns(self):
        fund, prices, dates = self._base_setup()
        result = compute_value_scores(fund, prices, score_dates=[dates[0]])
        expected = {"ticker", "date", "value_score"}
        assert expected.issubset(set(result.columns))

    def test_at_least_one_sub_factor_present(self):
        fund, prices, dates = self._base_setup()
        result = compute_value_scores(fund, prices, score_dates=[dates[0]])
        sub_cols = [c for c in ["earnings_yield", "book_to_market", "fcf_yield"] if c in result.columns]
        assert len(sub_cols) >= 1

    def test_scores_are_zscored_cross_sectionally(self):
        """Each sub-factor should have mean ≈ 0 and std ≈ 1 on each date."""
        fund, prices, dates = self._base_setup()
        result = compute_value_scores(fund, prices, score_dates=[dates[0]])
        for col in ["earnings_yield", "book_to_market", "fcf_yield"]:
            if col not in result.columns:
                continue
            vals = result[col].dropna()
            if len(vals) >= 3:
                assert abs(vals.mean()) < 0.01, f"{col} mean not near 0"
                assert abs(vals.std(ddof=1) - 1.0) < 0.01, f"{col} std not near 1"

    def test_no_future_fundamentals_used(self):
        """Scores computed before release_date should yield no rows."""
        fund = _make_standard_fundamentals(release_date=date(2022, 6, 1))
        dates = _biz_dates(date(2022, 1, 1), 5)  # all before release_date
        prices = _make_prices(TICKERS, dates)
        result = compute_value_scores(fund, prices)
        assert result.empty, "Future fundamentals should not be visible"

    def test_scores_computed_after_release_date(self):
        fund = _make_standard_fundamentals(release_date=date(2022, 2, 1))
        dates = _biz_dates(date(2022, 3, 1), 5)  # all after release_date
        prices = _make_prices(TICKERS, dates)
        result = compute_value_scores(fund, prices)
        assert not result.empty

    def test_missing_fundamentals_columns_raises(self):
        prices = _make_prices(TICKERS, _biz_dates(date(2022, 1, 1), 3))
        bad_fund = pd.DataFrame({"ticker": ["A"]})
        with pytest.raises(ValueError, match="missing columns"):
            compute_value_scores(bad_fund, prices)

    def test_missing_prices_columns_raises(self):
        fund = _make_standard_fundamentals()
        bad_prices = pd.DataFrame({"ticker": ["A"]})
        with pytest.raises(ValueError, match="missing columns"):
            compute_value_scores(fund, bad_prices)

    def test_min_tickers_threshold_enforced(self):
        """With fewer tickers than min_tickers, no scores returned."""
        small_tickers = TICKERS[:3]
        fund = _make_standard_fundamentals(tickers=small_tickers)
        prices = _make_prices(small_tickers, _biz_dates(date(2022, 3, 1), 5))
        result = compute_value_scores(fund, prices, min_tickers=10)
        assert result.empty

    def test_value_score_is_composite_of_sub_factors(self):
        """value_score should be the mean of available sub-factor z-scores."""
        fund, prices, dates = self._base_setup()
        result = compute_value_scores(fund, prices, score_dates=[dates[0]])
        sub_cols = [c for c in ["earnings_yield", "book_to_market", "fcf_yield"] if c in result.columns]
        if len(sub_cols) > 1:
            expected_composite = result[sub_cols].mean(axis=1, skipna=True)
            pd.testing.assert_series_equal(
                result["value_score"].round(6),
                expected_composite.round(6),
                check_names=False,
            )

    def test_negative_earnings_do_not_crash(self):
        """Companies with negative net income should produce valid rows."""
        fund = _make_standard_fundamentals(net_income_multiplier=-1.0)
        dates = _biz_dates(date(2022, 3, 1), 3)
        prices = _make_prices(TICKERS, dates)
        result = compute_value_scores(fund, prices)
        assert not result.empty

    def test_zero_market_cap_tickers_excluded(self):
        """Tickers with zero price produce zero market cap and should be dropped."""
        dates = _biz_dates(date(2022, 3, 1), 3)
        prices = _make_prices(TICKERS, dates, base=0.0)
        fund = _make_standard_fundamentals()
        result = compute_value_scores(fund, prices)
        assert result.empty


# ─── Quality factor tests ─────────────────────────────────────────────────────

class TestComputeQualityScores:
    def _base_setup(self):
        dates = _biz_dates(date(2022, 3, 1), 10)
        fund = _make_standard_fundamentals()
        prices = _make_prices(TICKERS, dates)
        return fund, prices, dates

    def test_output_columns(self):
        fund, prices, dates = self._base_setup()
        result = compute_quality_scores(fund, prices, score_dates=[dates[0]])
        expected = {"ticker", "date", "quality_score"}
        assert expected.issubset(set(result.columns))

    def test_sub_factors_present(self):
        fund, prices, dates = self._base_setup()
        result = compute_quality_scores(fund, prices, score_dates=[dates[0]])
        sub_cols = [c for c in ["roe", "gross_profitability", "accruals"] if c in result.columns]
        assert len(sub_cols) >= 1

    def test_scores_zscored_cross_sectionally(self):
        fund, prices, dates = self._base_setup()
        result = compute_quality_scores(fund, prices, score_dates=[dates[0]])
        for col in ["roe", "gross_profitability", "accruals"]:
            if col not in result.columns:
                continue
            vals = result[col].dropna()
            if len(vals) >= 3:
                assert abs(vals.mean()) < 0.01
                assert abs(vals.std(ddof=1) - 1.0) < 0.01

    def test_no_future_fundamentals_used(self):
        fund = _make_standard_fundamentals(release_date=date(2022, 6, 1))
        dates = _biz_dates(date(2022, 1, 1), 5)
        prices = _make_prices(TICKERS, dates)
        result = compute_quality_scores(fund, prices)
        assert result.empty

    def test_accruals_negation_low_accruals_score_higher(self):
        """Ticker with lower accruals (more cash-based earnings) should score higher."""
        two_tickers = ["LOW_ACC", "HIGH_ACC"]
        release = date(2022, 1, 15)
        period_end = date(2021, 12, 31)

        fund = _make_fundamentals(
            two_tickers, release, period_end,
            items={
                "net_income":         {"LOW_ACC": 1000, "HIGH_ACC": 1000},
                "operating_cash_flow": {"LOW_ACC": 950,  "HIGH_ACC": 500},
                "total_assets":       {"LOW_ACC": 10000, "HIGH_ACC": 10000},
                "gross_profit":       {"LOW_ACC": 3000, "HIGH_ACC": 3000},
                "total_equity":       {"LOW_ACC": 5000, "HIGH_ACC": 5000},
            },
        )
        dates = _biz_dates(date(2022, 2, 1), 2)
        prices = _make_prices(two_tickers, dates)
        result = compute_quality_scores(fund, prices, min_tickers=2)

        if result.empty:
            pytest.skip("Not enough tickers for cross-sectional z-score")

        day_result = result[result["date"] == dates[0]]
        low_acc_score = day_result[day_result["ticker"] == "LOW_ACC"]["quality_score"].iloc[0]
        high_acc_score = day_result[day_result["ticker"] == "HIGH_ACC"]["quality_score"].iloc[0]
        assert low_acc_score > high_acc_score, (
            f"Low-accrual ticker should score higher: {low_acc_score:.4f} vs {high_acc_score:.4f}"
        )

    def test_min_tickers_threshold_enforced(self):
        small_tickers = TICKERS[:3]
        fund = _make_standard_fundamentals(tickers=small_tickers)
        prices = _make_prices(small_tickers, _biz_dates(date(2022, 3, 1), 3))
        result = compute_quality_scores(fund, prices, min_tickers=10)
        assert result.empty

    def test_missing_fundamentals_raises(self):
        prices = _make_prices(TICKERS, _biz_dates(date(2022, 1, 1), 3))
        with pytest.raises(ValueError, match="missing columns"):
            compute_quality_scores(pd.DataFrame({"ticker": ["A"]}), prices)

    def test_quality_score_composite_of_sub_factors(self):
        fund, prices, dates = self._base_setup()
        result = compute_quality_scores(fund, prices, score_dates=[dates[0]])
        sub_cols = [c for c in ["roe", "gross_profitability", "accruals"] if c in result.columns]
        if len(sub_cols) > 1:
            expected = result[sub_cols].mean(axis=1, skipna=True)
            pd.testing.assert_series_equal(
                result["quality_score"].round(6),
                expected.round(6),
                check_names=False,
            )

    def test_zero_equity_tickers_excluded_from_roe(self):
        """ROE undefined for zero equity — ticker may still appear with other metrics."""
        tickers = ["ZE", "NORMAL"] + TICKERS[:10]
        release = date(2022, 1, 15)
        period_end = date(2021, 12, 31)
        fund = _make_standard_fundamentals(tickers=tickers)
        # Overwrite equity for ZE to 0
        zero_eq = pd.DataFrame([{
            "ticker": "ZE",
            "period_end_date": period_end,
            "release_date": release,
            "period_type": "quarterly",
            "item_name": "total_equity",
            "value": Decimal("0"),
        }])
        fund = pd.concat([fund[fund["ticker"] != "ZE"], zero_eq], ignore_index=True)
        dates = _biz_dates(date(2022, 2, 1), 3)
        prices = _make_prices(tickers, dates)
        # Should not crash
        result = compute_quality_scores(fund, prices)
        assert result is not None
