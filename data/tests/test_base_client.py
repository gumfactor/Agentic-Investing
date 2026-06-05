"""Tests for BaseMarketDataClient."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd
import pytest

from data.ingestion.market.base_client import BaseMarketDataClient, OHLCVBar, CorporateActionRecord


# Minimal concrete implementation for testing the base class contract.
class _ConcreteClient(BaseMarketDataClient):
    def fetch_ohlcv(self, tickers, start, end):
        return pd.DataFrame()

    def fetch_corporate_actions(self, tickers, start, end):
        return pd.DataFrame()


class TestValidateDateRange:
    def test_valid_range_does_not_raise(self) -> None:
        client = _ConcreteClient()
        client.validate_date_range(date(2024, 1, 1), date(2024, 1, 31))

    def test_same_day_does_not_raise(self) -> None:
        client = _ConcreteClient()
        client.validate_date_range(date(2024, 1, 1), date(2024, 1, 1))

    def test_start_after_end_raises(self) -> None:
        client = _ConcreteClient()
        with pytest.raises(ValueError, match="start"):
            client.validate_date_range(date(2024, 2, 1), date(2024, 1, 1))


class TestOHLCVBarDataclass:
    def test_is_frozen(self) -> None:
        bar = OHLCVBar(
            ticker="AAPL",
            date=date(2024, 1, 2),
            open=Decimal("150"),
            high=Decimal("155"),
            low=Decimal("149"),
            close=Decimal("152"),
            volume=1_000_000,
            source_adj_close=Decimal("151"),
            source="yfinance",
        )
        with pytest.raises(Exception):
            bar.ticker = "MSFT"  # type: ignore[misc]

    def test_optional_fields_accept_none(self) -> None:
        bar = OHLCVBar(
            ticker="AAPL",
            date=date(2024, 1, 2),
            open=None,
            high=None,
            low=None,
            close=Decimal("152"),
            volume=None,
            source_adj_close=None,
            source="yfinance",
        )
        assert bar.open is None
        assert bar.volume is None


class TestCorporateActionRecordDataclass:
    def test_fields_accessible(self) -> None:
        rec = CorporateActionRecord(
            ticker="AAPL",
            ex_date=date(2024, 6, 1),
            action_type="split",
            value=Decimal("4"),
            notes=None,
            source="yfinance",
        )
        assert rec.action_type == "split"
        assert rec.value == Decimal("4")
