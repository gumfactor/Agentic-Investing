"""Abstract interface for market data clients.

All concrete clients (yfinance, Polygon, etc.) must implement this interface.
Swapping data providers is a matter of changing the concrete class in config;
no consuming code changes are required.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class OHLCVBar:
    """A single daily price bar for one ticker.

    Prices use Decimal to avoid floating-point accumulation errors in
    financial arithmetic. The DB schema stores NUMERIC(18,6) for the same reason.
    """

    ticker: str
    date: date
    open: Optional[Decimal]
    high: Optional[Decimal]
    low: Optional[Decimal]
    close: Decimal
    volume: Optional[int]
    source_adj_close: Optional[Decimal]
    source: str


@dataclass(frozen=True)
class CorporateActionRecord:
    """A single corporate action event."""

    ticker: str
    ex_date: date
    # 'split' | 'dividend' | 'spinoff'
    action_type: str
    value: Decimal
    notes: Optional[str]
    source: str


class BaseMarketDataClient(ABC):
    """Abstract market data client.

    Concrete implementations must override fetch_ohlcv and fetch_corporate_actions.
    All returned DataFrames must conform to the column contracts documented below.
    """

    @abstractmethod
    def fetch_ohlcv(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch daily OHLCV bars for a list of tickers over a date range.

        Returns a DataFrame with columns:
            ticker (str), date (date), open (Decimal), high (Decimal),
            low (Decimal), close (Decimal), volume (int | None),
            source_adj_close (Decimal | None), source (str)

        Rows with invalid tickers are silently omitted (not an error).
        Raises ValueError if start > end.
        """
        ...

    @abstractmethod
    def fetch_corporate_actions(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch corporate actions (splits, dividends) for a list of tickers.

        Returns a DataFrame with columns:
            ticker (str), ex_date (date), action_type (str),
            value (Decimal), notes (str | None), source (str)
        """
        ...

    def validate_date_range(self, start: date, end: date) -> None:
        if start > end:
            raise ValueError(f"start ({start}) must be <= end ({end})")
