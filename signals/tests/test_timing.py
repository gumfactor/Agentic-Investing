"""Tests for signals/research/timing.py (BUG-009, design plan section 2)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from signals.research.timing import (
    DEFAULT_TIMING_POLICY,
    RETURN_SERIES_COLUMNS,
    SameDateScoreError,
    TimingPolicy,
    build_return_series,
    reject_same_date,
)


def _business_dates(start: date, n: int) -> list[date]:
    dates = []
    d = start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return dates


class TestTimingPolicy:
    def test_default_policy_id(self):
        assert DEFAULT_TIMING_POLICY.policy_id == "t_plus_1_close_v1"
        assert DEFAULT_TIMING_POLICY.execution_lag_sessions == 1

    def test_zero_lag_rejected(self):
        with pytest.raises(SameDateScoreError):
            TimingPolicy(policy_id="bad", execution_lag_sessions=0)

    def test_negative_lag_rejected(self):
        with pytest.raises(SameDateScoreError):
            TimingPolicy(policy_id="bad", execution_lag_sessions=-1)

    def test_custom_positive_lag_accepted(self):
        policy = TimingPolicy(policy_id="t_plus_2", execution_lag_sessions=2)
        assert policy.execution_lag_sessions == 2


class TestRejectSameDate:
    def test_forward_dates_pass(self):
        reject_same_date(date(2022, 1, 3), date(2022, 1, 4))  # no raise

    def test_same_date_raises(self):
        with pytest.raises(SameDateScoreError):
            reject_same_date(date(2022, 1, 3), date(2022, 1, 3))

    def test_backward_date_raises(self):
        with pytest.raises(SameDateScoreError):
            reject_same_date(date(2022, 1, 4), date(2022, 1, 3))


class TestBuildReturnSeries:
    def _prices(self, ticker: str, n: int, start_close: float = 100.0) -> pd.DataFrame:
        dates = _business_dates(date(2022, 1, 3), n)
        return pd.DataFrame(
            [{"ticker": ticker, "date": d, "close": start_close + i} for i, d in enumerate(dates)]
        )

    def test_output_columns(self):
        prices = self._prices("A", 10)
        result = build_return_series(prices, horizons=[1])
        assert list(result.columns) == RETURN_SERIES_COLUMNS

    def test_score_date_before_entry_date_before_exit_date(self):
        prices = self._prices("A", 15)
        result = build_return_series(prices, horizons=[1, 3])
        assert (result["score_date"] < result["entry_date"]).all()
        assert (result["entry_date"] < result["exit_date"]).all()

    def test_baseline_one_session_lag(self):
        dates = _business_dates(date(2022, 1, 3), 5)
        prices = pd.DataFrame(
            [{"ticker": "A", "date": d, "close": 100.0 + i * 10} for i, d in enumerate(dates)]
        )
        result = build_return_series(prices, horizons=[1])
        row = result[result["score_date"] == dates[0]].iloc[0]
        assert row["entry_date"] == dates[1]
        assert row["exit_date"] == dates[2]
        # entry close=110, exit close=120
        assert abs(row["forward_return"] - (120.0 / 110.0 - 1.0)) < 1e-9

    def test_horizon_extending_past_history_dropped(self):
        prices = self._prices("A", 5)
        result = build_return_series(prices, horizons=[10])
        assert result.empty

    def test_per_ticker_calendar_independence(self):
        """A gap in ticker B's calendar must not shift ticker A's entry/exit."""
        dates_a = _business_dates(date(2022, 1, 3), 12)
        dates_b = [d for i, d in enumerate(dates_a) if i not in (3, 4)]  # B missing 2 sessions
        prices = pd.concat([
            pd.DataFrame([{"ticker": "A", "date": d, "close": 100.0 + i} for i, d in enumerate(dates_a)]),
            pd.DataFrame([{"ticker": "B", "date": d, "close": 200.0 + i} for i, d in enumerate(dates_b)]),
        ], ignore_index=True)
        result = build_return_series(prices, horizons=[1])
        row_a = result[(result["ticker"] == "A") & (result["score_date"] == dates_a[0])].iloc[0]
        assert row_a["entry_date"] == dates_a[1]
        assert row_a["exit_date"] == dates_a[2]

    def test_multiple_tickers_independent_rows(self):
        prices = pd.concat([self._prices("A", 10), self._prices("B", 10, start_close=50.0)], ignore_index=True)
        result = build_return_series(prices, horizons=[1])
        assert set(result["ticker"].unique()) == {"A", "B"}

    def test_custom_timing_policy(self):
        dates = _business_dates(date(2022, 1, 3), 8)
        prices = pd.DataFrame(
            [{"ticker": "A", "date": d, "close": 100.0 + i} for i, d in enumerate(dates)]
        )
        policy = TimingPolicy(policy_id="t_plus_3", execution_lag_sessions=3)
        result = build_return_series(prices, horizons=[1], timing_policy=policy)
        row = result[result["score_date"] == dates[0]].iloc[0]
        assert row["entry_date"] == dates[3]
        assert row["exit_date"] == dates[4]
        assert row["timing_policy_id"] == "t_plus_3"

    def test_missing_columns_raises(self):
        prices = self._prices("A", 5).drop(columns=["close"])
        with pytest.raises(ValueError, match="missing columns"):
            build_return_series(prices, horizons=[1])

    def test_empty_prices_raises(self):
        with pytest.raises(ValueError, match="empty"):
            build_return_series(pd.DataFrame(columns=["ticker", "date", "close"]), horizons=[1])

    def test_non_positive_horizon_raises(self):
        prices = self._prices("A", 5)
        with pytest.raises(ValueError, match="horizon"):
            build_return_series(prices, horizons=[0])

    def test_duplicate_date_rows_deduplicated(self):
        dates = _business_dates(date(2022, 1, 3), 5)
        rows = [{"ticker": "A", "date": d, "close": 100.0 + i} for i, d in enumerate(dates)]
        # Duplicate the first row (e.g. an upstream join artifact).
        rows.append(dict(rows[0]))
        prices = pd.DataFrame(rows)
        result = build_return_series(prices, horizons=[1])
        # Still exactly one row per (score_date, horizon) for ticker A.
        assert len(result[result["score_date"] == dates[0]]) == 1
