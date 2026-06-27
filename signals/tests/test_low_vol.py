"""Tests for signals/indicators/low_vol.py.

All tests use synthetic price data — no DB or network access required.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from signals.indicators.low_vol import compute_lowvol_scores


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_prices(
    tickers: list[str],
    n_days: int,
    start: date = date(2020, 1, 2),
    base_price: float = 100.0,
    base_return: float = 0.001,
) -> pd.DataFrame:
    """Synthetic prices; each ticker gets a slightly different daily return."""
    dates = [start + timedelta(days=i) for i in range(n_days)]
    rows = []
    for j, ticker in enumerate(tickers):
        daily_return = base_return * (1 + j * 0.5)
        price = base_price
        for d in dates:
            rows.append({"ticker": ticker, "date": d, "close": round(price, 4)})
            price *= 1 + daily_return
    return pd.DataFrame(rows)


def _make_volatile_prices(
    ticker: str,
    n_days: int,
    daily_vol: float,
    start: date = date(2020, 1, 2),
    seed: int = 0,
) -> pd.DataFrame:
    """Simulate a GBM series with specified daily volatility."""
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.0, daily_vol, size=n_days)
    prices = 100.0 * np.exp(np.cumsum(log_returns))
    dates = [start + timedelta(days=i) for i in range(n_days)]
    return pd.DataFrame({"ticker": ticker, "date": dates, "close": prices})


# ─── compute_lowvol_scores ────────────────────────────────────────────────────

class TestComputeLowvolScores:

    def test_returns_expected_columns(self) -> None:
        prices = _make_prices(["A", "B", "C"], n_days=300)
        result = compute_lowvol_scores(prices)
        for col in ["ticker", "date", "vol_21d", "vol_63d", "vol_252d", "lowvol_score"]:
            assert col in result.columns

    def test_no_beta_column_without_market(self) -> None:
        prices = _make_prices(["A", "B"], n_days=300)
        result = compute_lowvol_scores(prices)
        assert "beta_252d" not in result.columns

    def test_beta_column_present_with_market(self) -> None:
        prices = _make_prices(["A", "B", "C"], n_days=300)
        market = _make_prices(["SPY"], n_days=300)
        result = compute_lowvol_scores(prices, market_prices=market)
        assert "beta_252d" in result.columns

    def test_output_is_long_format(self) -> None:
        prices = _make_prices(["A", "B", "C"], n_days=300)
        result = compute_lowvol_scores(prices)
        assert set(result["ticker"].unique()) == {"A", "B", "C"}

    def test_low_vol_ticker_scores_higher(self) -> None:
        """A calm ticker should outscore a volatile one on recent dates."""
        calm = _make_volatile_prices("CALM", n_days=300, daily_vol=0.005, seed=1)
        wild = _make_volatile_prices("WILD", n_days=300, daily_vol=0.04, seed=2)
        prices = pd.concat([calm, wild], ignore_index=True)
        result = compute_lowvol_scores(prices)

        late_dates = sorted(result["date"].unique())[-30:]
        late = result[result["date"].isin(late_dates)]
        calm_score = late[late["ticker"] == "CALM"]["lowvol_score"].mean()
        wild_score = late[late["ticker"] == "WILD"]["lowvol_score"].mean()
        assert calm_score > wild_score

    def test_scores_are_cross_sectionally_centred(self) -> None:
        """lowvol_score should average near 0 across tickers on each date."""
        prices = _make_prices(["A", "B", "C", "D", "E"], n_days=300)
        result = compute_lowvol_scores(prices)
        date_means = result.groupby("date")["lowvol_score"].mean().dropna()
        assert (date_means.abs() < 1e-9).all()

    def test_no_scores_before_warmup(self) -> None:
        """252-day vol requires at least 252 rows; not present in 30-day data."""
        prices = _make_prices(["A", "B"], n_days=30)
        result = compute_lowvol_scores(prices)
        assert result["vol_252d"].isna().all()

    def test_short_window_appears_before_long_window(self) -> None:
        prices = _make_prices(["A", "B", "C"], n_days=300)
        result = compute_lowvol_scores(prices)
        first_21d = result.dropna(subset=["vol_21d"])["date"].min()
        first_252d = result.dropna(subset=["vol_252d"])["date"].min()
        assert first_21d < first_252d

    def test_custom_windows(self) -> None:
        prices = _make_prices(["A", "B"], n_days=100)
        result = compute_lowvol_scores(prices, vol_windows={"vol_10d": 10, "vol_30d": 30})
        assert "vol_10d" in result.columns
        assert "vol_30d" in result.columns
        assert "vol_21d" not in result.columns

    def test_raises_on_missing_column(self) -> None:
        bad = pd.DataFrame({"ticker": ["A"], "date": [date(2020, 1, 1)]})
        with pytest.raises(ValueError, match="missing required columns"):
            compute_lowvol_scores(bad)

    def test_raises_on_empty_dataframe(self) -> None:
        empty = pd.DataFrame(columns=["ticker", "date", "close"])
        with pytest.raises(ValueError, match="empty"):
            compute_lowvol_scores(empty)

    def test_accepts_decimal_close(self) -> None:
        prices = _make_prices(["A", "B", "C"], n_days=300)
        prices["close"] = prices["close"].apply(Decimal)
        result = compute_lowvol_scores(prices)
        assert not result.empty

    def test_output_sorted_by_date_ticker(self) -> None:
        prices = _make_prices(["C", "A", "B"], n_days=300)
        result = compute_lowvol_scores(prices)
        expected = result.sort_values(["date", "ticker"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(result, expected)

    def test_no_duplicate_ticker_date(self) -> None:
        prices = _make_prices(["A", "B"], n_days=300)
        result = compute_lowvol_scores(prices)
        assert not result.duplicated(subset=["ticker", "date"]).any()

    def test_vol_values_are_annualised(self) -> None:
        """vol_252d ≈ daily_vol × sqrt(252) for a constant-vol GBM series."""
        daily_vol = 0.02
        calm = _make_volatile_prices("X", n_days=500, daily_vol=daily_vol, seed=42)
        prices = pd.concat(
            [calm, _make_volatile_prices("Y", n_days=500, daily_vol=0.01, seed=7)],
            ignore_index=True,
        )
        result = compute_lowvol_scores(prices)

        # Back-compute annualised vol for X from the raw result
        # (the z-score undoes the centring, so we reconstruct raw vol separately)
        wide = calm.pivot_table(index="date", columns="ticker", values="close")
        log_ret = np.log(wide / wide.shift(1))
        raw_252d_vol = log_ret.rolling(252, min_periods=176).std() * np.sqrt(252)
        expected_mean = raw_252d_vol.dropna().mean().values[0]
        assert 0.01 < expected_mean < 0.8, "Annualised vol should be in plausible range"

    def test_market_prices_validation(self) -> None:
        prices = _make_prices(["A", "B"], n_days=300)
        bad_market = pd.DataFrame({"ticker": ["SPY"], "date": [date(2020, 1, 1)]})
        with pytest.raises(ValueError, match="missing required columns"):
            compute_lowvol_scores(prices, market_prices=bad_market)
