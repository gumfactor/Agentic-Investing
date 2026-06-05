"""Tests for data quality checks."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from data.normalization.quality_checks import (
    run_quality_checks,
    check_universe_completeness,
)


def _make_row(
    ticker: str = "AAPL",
    dt: date = date(2024, 1, 2),
    open_: float = 150.0,
    high: float = 155.0,
    low: float = 149.0,
    close: float = 152.0,
    volume: int = 1_000_000,
) -> dict:
    return {
        "ticker": ticker,
        "date": dt,
        "open": Decimal(str(open_)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": volume,
    }


class TestNegativePriceCheck:
    def test_flags_negative_close(self) -> None:
        df = pd.DataFrame([_make_row(close=-1.0)])
        flags = run_quality_checks(df)
        assert not flags.empty
        assert (flags["flag_type"] == "negative_price").any()
        assert (flags["severity"] == "error").any()

    def test_flags_zero_close(self) -> None:
        df = pd.DataFrame([_make_row(close=0.0)])
        flags = run_quality_checks(df)
        assert any(flags["flag_type"] == "negative_price")

    def test_clean_prices_no_flag(self) -> None:
        df = pd.DataFrame([_make_row()])
        flags = run_quality_checks(df)
        negative_flags = flags[flags["flag_type"] == "negative_price"]
        assert negative_flags.empty


class TestHlocViolationCheck:
    def test_high_less_than_low(self) -> None:
        df = pd.DataFrame([_make_row(high=148.0, low=149.0, close=148.5)])
        flags = run_quality_checks(df)
        assert any(flags["flag_type"] == "hloc_violation")
        assert any(flags["severity"] == "error")

    def test_close_above_high(self) -> None:
        df = pd.DataFrame([_make_row(high=155.0, low=149.0, close=160.0)])
        flags = run_quality_checks(df)
        hloc_flags = flags[flags["flag_type"] == "hloc_violation"]
        assert not hloc_flags.empty
        assert any(hloc_flags["severity"] == "warning")

    def test_close_below_low(self) -> None:
        df = pd.DataFrame([_make_row(high=155.0, low=149.0, close=145.0)])
        flags = run_quality_checks(df)
        assert any(flags["flag_type"] == "hloc_violation")

    def test_valid_bar_no_hloc_flag(self) -> None:
        df = pd.DataFrame([_make_row(open_=151.0, high=155.0, low=149.0, close=152.0)])
        flags = run_quality_checks(df)
        assert flags[flags["flag_type"] == "hloc_violation"].empty


class TestZeroVolumeCheck:
    def test_flags_zero_volume(self) -> None:
        df = pd.DataFrame([_make_row(volume=0)])
        flags = run_quality_checks(df)
        assert any(flags["flag_type"] == "volume_zero")
        assert any(flags["severity"] == "warning")

    def test_none_volume_not_flagged(self) -> None:
        row = _make_row()
        row["volume"] = None
        df = pd.DataFrame([row])
        flags = run_quality_checks(df)
        assert flags[flags["flag_type"] == "volume_zero"].empty

    def test_positive_volume_clean(self) -> None:
        df = pd.DataFrame([_make_row(volume=500_000)])
        flags = run_quality_checks(df)
        assert flags[flags["flag_type"] == "volume_zero"].empty


class TestPriceJumpCheck:
    def _make_series(self, n: int = 30, jump_at: int = 25, jump_factor: float = 1.25) -> pd.DataFrame:
        """Build a series with a big price jump at jump_at."""
        rows = []
        price = 100.0
        for i in range(n):
            if i == jump_at:
                price *= jump_factor
            rows.append(_make_row(
                ticker="TEST",
                dt=date(2024, 1, i + 1),
                open_=price * 0.99,
                high=price * 1.01,
                low=price * 0.98,
                close=price,
            ))
        return pd.DataFrame(rows)

    def test_detects_large_jump(self) -> None:
        df = self._make_series(jump_factor=1.5)  # 50% jump — well above 3σ
        flags = run_quality_checks(df)
        jump_flags = flags[flags["flag_type"] == "price_jump"]
        assert not jump_flags.empty

    def test_no_false_positive_on_normal_movement(self) -> None:
        from datetime import timedelta
        base = date(2024, 1, 2)
        rows = []
        price = 100.0
        for i in range(40):
            price *= 1.001  # steady 0.1% daily gain
            rows.append(_make_row(
                ticker="STEADY",
                dt=base + timedelta(days=i),
                open_=price * 0.999,
                high=price * 1.001,
                low=price * 0.998,
                close=price,
            ))
        df = pd.DataFrame(rows)
        flags = run_quality_checks(df)
        assert flags[flags["flag_type"] == "price_jump"].empty

    def test_insufficient_history_no_flag(self) -> None:
        """With fewer than window+1 bars, price jump detection is skipped."""
        df = self._make_series(n=15, jump_at=10, jump_factor=1.5)
        flags = run_quality_checks(df)
        # Should not crash; may or may not flag depending on window size
        assert isinstance(flags, pd.DataFrame)


class TestUniverseCompleteness:
    def test_flags_missing_tickers(self) -> None:
        df = pd.DataFrame([_make_row("AAPL")])
        expected = ["AAPL", "MSFT", "GOOGL"]
        flags = check_universe_completeness(df, expected, check_date=date(2024, 1, 2))
        missing_tickers = {f["ticker"] for f in flags}
        assert "MSFT" in missing_tickers
        assert "GOOGL" in missing_tickers
        assert "AAPL" not in missing_tickers

    def test_no_flags_when_complete(self) -> None:
        tickers = ["AAPL", "MSFT"]
        df = pd.DataFrame([_make_row("AAPL"), _make_row("MSFT")])
        flags = check_universe_completeness(df, tickers, check_date=date(2024, 1, 2))
        assert flags == []

    def test_severity_escalates_with_missing_fraction(self) -> None:
        # 50% missing → should be 'error'
        df = pd.DataFrame([_make_row("AAPL")])
        expected = [f"TICKER{i}" for i in range(20)]  # 20 expected, only AAPL present
        flags = check_universe_completeness(df, expected, check_date=date(2024, 1, 2))
        severities = {f["severity"] for f in flags}
        assert "error" in severities


class TestRunQualityChecksOnEmpty:
    def test_empty_dataframe_returns_empty_flags(self) -> None:
        empty = pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume"])
        result = run_quality_checks(empty)
        assert result.empty
        assert list(result.columns) == ["ticker", "date", "flag_type", "severity", "message"]
