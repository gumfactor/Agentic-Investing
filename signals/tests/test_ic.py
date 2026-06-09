"""Tests for signals/research/ic.py."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from signals.research.ic import (
    chronological_split,
    compute_forward_returns,
    compute_ic_series,
    multiple_testing_correction,
    summarize_ic,
)


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

def _business_dates(start: date, n: int) -> list[date]:
    """Generate n weekday dates starting from start."""
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


def _make_prices(tickers: list[str], n_days: int, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic log-normal price series."""
    rng = np.random.default_rng(seed)
    dates = _business_dates(date(2021, 1, 4), n_days)
    rows = []
    for i, ticker in enumerate(tickers):
        price = 100.0
        daily_drift = 0.0005 * (i - len(tickers) / 2)  # cross-sectional spread
        for d in dates:
            ret = daily_drift + rng.normal(0, 0.015)
            price *= np.exp(ret)
            rows.append({"ticker": ticker, "date": d, "close": price})
    return pd.DataFrame(rows)


def _make_scores(
    prices: pd.DataFrame,
    score_col: str = "score",
    seed: int = 0,
) -> pd.DataFrame:
    """Assign random cross-sectional scores that have a weak signal."""
    rng = np.random.default_rng(seed)
    rows = []
    for d, group in prices.groupby("date"):
        tickers = group["ticker"].tolist()
        scores = rng.standard_normal(len(tickers))
        for ticker, s in zip(tickers, scores):
            rows.append({"ticker": ticker, "date": d, score_col: s})
    return pd.DataFrame(rows)


def _make_predictive_scores(
    prices: pd.DataFrame,
    horizon: int,
    score_col: str = "score",
    noise: float = 0.5,
    seed: int = 7,
) -> pd.DataFrame:
    """Scores that are correlated with future returns (positive IC expected)."""
    rng = np.random.default_rng(seed)
    wide = (
        prices.pivot_table(index="date", columns="ticker", values="close")
        .sort_index()
    )
    wide.columns.name = None
    fwd_ret = (wide.shift(-horizon) / wide - 1.0).dropna()

    rows = []
    for d, ret_row in fwd_ret.iterrows():
        valid = ret_row.dropna()
        if len(valid) < 5:
            continue
        signal = valid + noise * pd.Series(rng.standard_normal(len(valid)), index=valid.index)
        for ticker, s in signal.items():
            rows.append({"ticker": ticker, "date": d, score_col: float(s)})
    return pd.DataFrame(rows)


# ─── compute_forward_returns ──────────────────────────────────────────────────

class TestComputeForwardReturns:
    def test_output_columns(self):
        prices = _make_prices(["A", "B", "C"], 50)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert set(result.columns) == {"ticker", "date", "horizon_days", "forward_return"}

    def test_both_horizons_present(self):
        prices = _make_prices(["A", "B"], 30)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert set(result["horizon_days"].unique()) == {1, 5}

    def test_no_nan_forward_returns(self):
        prices = _make_prices(["A", "B", "C"], 30)
        result = compute_forward_returns(prices, horizons=[1, 5])
        assert result["forward_return"].notna().all()

    def test_last_h_dates_absent(self):
        """The last h dates cannot have h-day forward returns."""
        prices = _make_prices(["A"], 20)
        result = compute_forward_returns(prices, horizons=[5])
        all_dates = sorted(prices["date"].unique())
        result_dates = set(result[result["horizon_days"] == 5]["date"].unique())
        for d in all_dates[-5:]:
            assert d not in result_dates

    def test_correct_return_magnitude(self):
        """Manual check: 1-day forward return = price[t+1]/price[t] - 1."""
        dates = _business_dates(date(2022, 1, 3), 5)
        prices_data = [{"ticker": "X", "date": d, "close": float(100 + i * 10)} for i, d in enumerate(dates)]
        prices = pd.DataFrame(prices_data)
        result = compute_forward_returns(prices, horizons=[1])
        row = result[result["date"] == dates[0]].iloc[0]
        expected = 110.0 / 100.0 - 1.0
        assert abs(row["forward_return"] - expected) < 1e-9

    def test_empty_horizons_returns_empty(self):
        prices = _make_prices(["A"], 10)
        result = compute_forward_returns(prices, horizons=[])
        assert result.empty

    def test_missing_column_raises(self):
        prices = _make_prices(["A"], 10).drop(columns=["close"])
        with pytest.raises(ValueError, match="missing columns"):
            compute_forward_returns(prices, horizons=[1])


# ─── compute_ic_series ────────────────────────────────────────────────────────

class TestComputeICSeries:
    def test_output_columns(self):
        prices = _make_prices(["A", "B", "C", "D", "E", "F"], 100)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1, 5])
        assert set(result.columns) == {"date", "horizon_days", "ic", "rank_ic", "n_obs"}

    def test_both_horizons_present(self):
        prices = _make_prices(list("ABCDEFGHIJ"), 80)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1, 5])
        assert {1, 5}.issubset(set(result["horizon_days"].unique()))

    def test_ic_in_valid_range(self):
        prices = _make_prices(list("ABCDEFGHIJ"), 80)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        assert result["ic"].between(-1.0, 1.0).all()
        assert result["rank_ic"].between(-1.0, 1.0).all()

    def test_predictive_scores_yield_positive_ic(self):
        """Scores constructed to predict forward returns should have positive mean IC."""
        prices = _make_prices(list("ABCDEFGHIJKLMNOP"), 300, seed=1)
        scores = _make_predictive_scores(prices, horizon=5, noise=0.3)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[5])
        mean_ic = result[result["horizon_days"] == 5]["ic"].mean()
        assert mean_ic > 0.0, f"Expected positive mean IC for predictive scores, got {mean_ic:.4f}"

    def test_random_scores_near_zero_ic(self):
        """Truly random scores should produce mean IC near 0."""
        prices = _make_prices(list("ABCDEFGHIJKLMNOP"), 400, seed=2)
        scores = _make_scores(prices, seed=99)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[5])
        mean_ic = result["ic"].mean()
        assert abs(mean_ic) < 0.15, f"Expected near-zero mean IC, got {mean_ic:.4f}"

    def test_fewer_than_min_tickers_excluded(self):
        """Dates with fewer than 5 tickers should be excluded from IC series."""
        prices = _make_prices(["A", "B", "C"], 30)  # only 3 tickers
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score", horizons=[1])
        assert result.empty

    def test_missing_score_col_raises(self):
        prices = _make_prices(["A", "B"], 20)
        scores = _make_scores(prices)
        with pytest.raises(ValueError, match="missing columns"):
            compute_ic_series(scores, prices, score_col="nonexistent", horizons=[1])

    def test_default_horizons_used(self):
        prices = _make_prices(list("ABCDEFGHIJ"), 120)
        scores = _make_scores(prices)
        result = compute_ic_series(scores, prices, score_col="score")
        # At minimum the 1-day horizon should appear (63-day needs more data)
        assert 1 in result["horizon_days"].values


# ─── summarize_ic ─────────────────────────────────────────────────────────────

class TestSummarizeIC:
    def _make_ic_series(self, n_dates: int = 50, horizons: list[int] = None) -> pd.DataFrame:
        if horizons is None:
            horizons = [1, 21]
        rng = np.random.default_rng(0)
        dates = _business_dates(date(2021, 1, 4), n_dates)
        rows = []
        for d in dates:
            for h in horizons:
                rows.append({
                    "date": d,
                    "horizon_days": h,
                    "ic": float(rng.normal(0.04, 0.10)),
                    "rank_ic": float(rng.normal(0.03, 0.10)),
                    "n_obs": 100,
                })
        return pd.DataFrame(rows)

    def test_output_columns(self):
        ic_series = self._make_ic_series()
        result = summarize_ic(ic_series, factor_name="test_factor")
        expected_cols = {
            "factor_name", "strategy_id", "eval_date", "horizon_days",
            "ic", "rank_ic", "ic_tstat", "ic_ir", "ic_pvalue", "n_observations",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_one_row_per_horizon(self):
        ic_series = self._make_ic_series(horizons=[1, 21])
        result = summarize_ic(ic_series, factor_name="f")
        assert len(result) == 2
        assert set(result["horizon_days"]) == {1, 21}

    def test_factor_name_propagated(self):
        ic_series = self._make_ic_series()
        result = summarize_ic(ic_series, factor_name="my_factor")
        assert (result["factor_name"] == "my_factor").all()

    def test_eval_date_defaults_to_max(self):
        ic_series = self._make_ic_series(n_dates=30)
        max_date = ic_series["date"].max()
        result = summarize_ic(ic_series, factor_name="f")
        assert (result["eval_date"] == max_date).all()

    def test_eval_date_can_be_overridden(self):
        ic_series = self._make_ic_series()
        custom_date = date(2025, 1, 1)
        result = summarize_ic(ic_series, factor_name="f", eval_date=custom_date)
        assert (result["eval_date"] == custom_date).all()

    def test_positive_mean_ic_yields_positive_tstat(self):
        """A consistently positive IC time series should have a positive t-stat."""
        dates = _business_dates(date(2020, 1, 2), 60)
        ic_series = pd.DataFrame({
            "date": dates,
            "horizon_days": 21,
            "ic": [0.05] * 60,
            "rank_ic": [0.04] * 60,
            "n_obs": 200,
        })
        result = summarize_ic(ic_series, factor_name="f")
        assert result.iloc[0]["ic_tstat"] > 0

    def test_empty_input_returns_empty(self):
        result = summarize_ic(pd.DataFrame(columns=["date", "horizon_days", "ic", "rank_ic", "n_obs"]), "f")
        assert result.empty

    def test_insufficient_dates_excluded(self):
        """Horizons with fewer than _MIN_IC_DATES_FOR_TSTAT obs are excluded."""
        dates = _business_dates(date(2021, 1, 4), 5)  # only 5 dates
        ic_series = pd.DataFrame({
            "date": dates,
            "horizon_days": 21,
            "ic": [0.04] * 5,
            "rank_ic": [0.03] * 5,
            "n_obs": 50,
        })
        result = summarize_ic(ic_series, factor_name="f")
        assert result.empty


# ─── multiple_testing_correction ─────────────────────────────────────────────

class TestMultipleTestingCorrection:
    def _make_summaries(self, pvalues: list[float]) -> pd.DataFrame:
        return pd.DataFrame({
            "factor_name": [f"f{i}" for i in range(len(pvalues))],
            "ic_pvalue": pvalues,
        })

    def test_adds_corrected_pvalue_and_significant_columns(self):
        summaries = self._make_summaries([0.001, 0.01, 0.05, 0.5])
        result = multiple_testing_correction(summaries)
        assert "corrected_pvalue" in result.columns
        assert "significant" in result.columns

    def test_very_small_pvalue_significant(self):
        """A p-value of 0.0001 should survive any reasonable multiple testing correction."""
        summaries = self._make_summaries([0.0001, 0.3, 0.5, 0.7, 0.9])
        result = multiple_testing_correction(summaries, method="bhy", alpha=0.05)
        assert result[result["ic_pvalue"] == 0.0001]["significant"].iloc[0]

    def test_large_pvalues_not_significant(self):
        summaries = self._make_summaries([0.5, 0.6, 0.7, 0.8, 0.9])
        result = multiple_testing_correction(summaries)
        assert not result["significant"].any()

    def test_bh_and_bhy_both_work(self):
        summaries = self._make_summaries([0.001, 0.01, 0.05, 0.5])
        for method in ("bh", "bhy"):
            result = multiple_testing_correction(summaries, method=method)
            assert "significant" in result.columns

    def test_invalid_method_raises(self):
        summaries = self._make_summaries([0.05])
        with pytest.raises(ValueError, match="method"):
            multiple_testing_correction(summaries, method="bonferroni")

    def test_missing_pvalue_column_raises(self):
        with pytest.raises(ValueError, match="ic_pvalue"):
            multiple_testing_correction(pd.DataFrame({"ic": [0.04]}))

    def test_bhy_more_conservative_than_bh(self):
        """BHY should reject fewer or equal hypotheses compared to BH."""
        summaries = self._make_summaries([0.001, 0.005, 0.01, 0.04, 0.06, 0.2])
        bh = multiple_testing_correction(summaries, method="bh")
        bhy = multiple_testing_correction(summaries, method="bhy")
        assert bh["significant"].sum() >= bhy["significant"].sum()


# ─── chronological_split ──────────────────────────────────────────────────────

class TestChronologicalSplit:
    def _make_scores(self, n_dates: int) -> pd.DataFrame:
        dates = _business_dates(date(2021, 1, 4), n_dates)
        return pd.DataFrame({
            "ticker": ["A"] * n_dates,
            "date": dates,
            "score": range(n_dates),
        })

    def test_no_overlap_between_train_and_val(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        train_dates = set(train["date"])
        val_dates = set(val["date"])
        assert train_dates.isdisjoint(val_dates)

    def test_train_val_cover_all_dates(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        all_dates = set(scores["date"])
        assert train["date"].tolist() or val["date"].tolist()
        assert set(train["date"]) | set(val["date"]) == all_dates

    def test_train_precedes_val(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        assert max(train["date"]) < min(val["date"])

    def test_approximate_split_fraction(self):
        scores = self._make_scores(100)
        train, val = chronological_split(scores, train_fraction=0.7)
        n_total = len(scores["date"].unique())
        n_train = len(train["date"].unique())
        assert abs(n_train / n_total - 0.7) < 0.05

    def test_fraction_one_puts_everything_in_train(self):
        scores = self._make_scores(20)
        train, val = chronological_split(scores, train_fraction=1.0)
        assert len(train) == len(scores)
        assert len(val) == 0
