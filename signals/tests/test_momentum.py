"""Tests for signals/indicators/momentum.py.

All tests use synthetic price data — no DB or network access required.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from signals.indicators.momentum import compute_momentum_scores, rank_by_momentum


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_prices(
    tickers: list[str],
    n_days: int,
    start: date = date(2020, 1, 2),
    base_price: float = 100.0,
    base_return: float = 0.001,
) -> pd.DataFrame:
    """Build a synthetic long-format price DataFrame.

    Each ticker gets a slightly different daily return so cross-sectional
    variance is non-zero and z-scores are well-defined.
    """
    dates = [start + timedelta(days=i) for i in range(n_days)]
    rows = []
    for j, ticker in enumerate(tickers):
        daily_return = base_return * (1 + j * 0.5)  # 0.001, 0.0015, 0.002 ...
        price = base_price
        for d in dates:
            rows.append({"ticker": ticker, "date": d, "close": round(price, 4)})
            price *= 1 + daily_return
    return pd.DataFrame(rows)


def _make_diverging_prices(n_days: int = 300) -> pd.DataFrame:
    """Two tickers with opposite trends so they land in different momentum buckets."""
    dates = [date(2020, 1, 2) + timedelta(days=i) for i in range(n_days)]
    rows = []
    up_price = down_price = 100.0
    for d in dates:
        rows.append({"ticker": "UP", "date": d, "close": round(up_price, 4)})
        rows.append({"ticker": "DOWN", "date": d, "close": round(down_price, 4)})
        up_price *= 1.002
        down_price *= 0.998
    return pd.DataFrame(rows)


# ─── compute_momentum_scores ──────────────────────────────────────────────────

class TestComputeMomentumScores:

    def test_returns_expected_columns(self) -> None:
        prices = _make_prices(["A", "B", "C"], n_days=300)
        result = compute_momentum_scores(prices)
        assert "ticker" in result.columns
        assert "date" in result.columns
        assert "momentum_score" in result.columns
        for w in ["mom_1m", "mom_3m", "mom_6m", "mom_12m"]:
            assert w in result.columns

    def test_output_is_long_format(self) -> None:
        prices = _make_prices(["A", "B"], n_days=300)
        result = compute_momentum_scores(prices)
        assert set(result["ticker"].unique()) == {"A", "B"}

    def test_scores_are_cross_sectionally_centred(self) -> None:
        """On each date, z-scores should sum near zero across tickers."""
        prices = _make_prices(["A", "B", "C", "D", "E"], n_days=300)
        result = compute_momentum_scores(prices)
        for window in ["mom_1m", "mom_3m", "mom_6m", "mom_12m"]:
            date_means = result.groupby("date")[window].mean().dropna()
            assert (date_means.abs() < 1e-9).all(), (
                f"Window {window} is not cross-sectionally centred"
            )

    def test_winner_has_positive_score_loser_negative(self) -> None:
        """UP ticker (strong trend) should outscore DOWN ticker on recent dates."""
        prices = _make_diverging_prices(n_days=300)
        result = compute_momentum_scores(prices)
        late_dates = sorted(result["date"].unique())[-30:]
        late = result[result["date"].isin(late_dates)]
        up_scores = late[late["ticker"] == "UP"]["momentum_score"].mean()
        down_scores = late[late["ticker"] == "DOWN"]["momentum_score"].mean()
        assert up_scores > down_scores

    def test_no_scores_before_warmup_period(self) -> None:
        """With only 30 days of data, no score requiring 252-day window should appear."""
        prices = _make_prices(["A", "B"], n_days=30)
        result = compute_momentum_scores(prices)
        # mom_12m requires 252 + 21 days of data; should be all NaN for 30-day input
        assert result["mom_12m"].isna().all()

    def test_short_window_scores_appear_before_long_windows(self) -> None:
        """1-month window should have data before the 12-month window."""
        prices = _make_prices(["A", "B", "C"], n_days=300)
        result = compute_momentum_scores(prices)
        first_1m = result.dropna(subset=["mom_1m"])["date"].min()
        first_12m = result.dropna(subset=["mom_12m"])["date"].min()
        assert first_1m < first_12m

    def test_custom_windows(self) -> None:
        prices = _make_prices(["A", "B"], n_days=200)
        result = compute_momentum_scores(prices, windows={"w20": 20, "w40": 40})
        assert "w20" in result.columns
        assert "w40" in result.columns
        assert "mom_1m" not in result.columns

    def test_raises_on_missing_column(self) -> None:
        bad = pd.DataFrame({"ticker": ["A"], "date": [date(2020, 1, 1)]})
        with pytest.raises(ValueError, match="missing required columns"):
            compute_momentum_scores(bad)

    def test_raises_on_empty_dataframe(self) -> None:
        empty = pd.DataFrame(columns=["ticker", "date", "close"])
        with pytest.raises(ValueError, match="empty"):
            compute_momentum_scores(empty)

    def test_accepts_decimal_close(self) -> None:
        """Decimal-typed close column (from DB) should be handled without error."""
        from decimal import Decimal
        prices = _make_prices(["A", "B"], n_days=300)
        prices["close"] = prices["close"].apply(Decimal)
        result = compute_momentum_scores(prices)
        assert not result.empty

    def test_output_sorted_by_date_ticker(self) -> None:
        prices = _make_prices(["C", "A", "B"], n_days=300)
        result = compute_momentum_scores(prices)
        expected_order = result.sort_values(["date", "ticker"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(result, expected_order)

    def test_composite_is_mean_of_available_windows(self) -> None:
        """For a row with all four windows populated, composite = simple mean."""
        prices = _make_prices(["A", "B", "C"], n_days=300)
        result = compute_momentum_scores(prices)
        fully_scored = result.dropna(subset=["mom_1m", "mom_3m", "mom_6m", "mom_12m"])
        if fully_scored.empty:
            pytest.skip("No fully-scored rows in synthetic data")
        row = fully_scored.iloc[0]
        expected = np.mean([row["mom_1m"], row["mom_3m"], row["mom_6m"], row["mom_12m"]])
        assert abs(row["momentum_score"] - expected) < 1e-10

    def test_deduplication_no_duplicate_ticker_date(self) -> None:
        prices = _make_prices(["A", "B"], n_days=300)
        result = compute_momentum_scores(prices)
        assert not result.duplicated(subset=["ticker", "date"]).any()


# ─── rank_by_momentum ─────────────────────────────────────────────────────────

class TestRankByMomentum:

    def _scored(self, n_tickers: int = 10) -> pd.DataFrame:
        tickers = [f"T{i:02d}" for i in range(n_tickers)]
        prices = _make_prices(tickers, n_days=300)
        return compute_momentum_scores(prices)

    def test_rank_column_present(self) -> None:
        result = rank_by_momentum(self._scored())
        assert "rank" in result.columns

    def test_bucket_column_present(self) -> None:
        result = rank_by_momentum(self._scored())
        assert "bucket" in result.columns

    def test_top_n_labelled_long(self) -> None:
        n_tickers = 10
        scores = self._scored(n_tickers)
        result = rank_by_momentum(scores, n_long=3, n_short=3)
        long_count_per_date = (
            result[result["bucket"] == "long"].groupby("date")["ticker"].count()
        )
        assert (long_count_per_date <= 3).all()

    def test_bottom_n_labelled_short(self) -> None:
        n_tickers = 10
        scores = self._scored(n_tickers)
        result = rank_by_momentum(scores, n_long=3, n_short=3)
        short_count_per_date = (
            result[result["bucket"] == "short"].groupby("date")["ticker"].count()
        )
        assert (short_count_per_date <= 3).all()

    def test_rank_1_is_highest_score(self) -> None:
        scores = self._scored()
        result = rank_by_momentum(scores)
        for _, group in result.dropna(subset=["rank", "momentum_score"]).groupby("date"):
            top = group[group["rank"] == 1]["momentum_score"].values
            if len(top):
                assert top[0] == group["momentum_score"].max()

    def test_raises_on_missing_score_col(self) -> None:
        scores = self._scored()
        with pytest.raises(ValueError, match="score_col"):
            rank_by_momentum(scores, score_col="nonexistent")
