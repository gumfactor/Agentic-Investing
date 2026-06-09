"""Tests for the yfinance market data client.

These tests mock the yfinance API so they run without network access.
They test the normalisation logic, error handling, and date contract.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data.ingestion.market.yfinance_client import (
    YFinanceClient,
    _normalise_yf_actions,
    _normalise_yf_download,
    _yf_fetch,
)


# ─── _normalise_yf_download unit tests ───────────────────────────────────────

class TestNormaliseYfDownload:
    def _single_ticker_raw(self) -> pd.DataFrame:
        """Simulate yfinance output for a single ticker (flat columns)."""
        idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
        return pd.DataFrame(
            {
                "Open": [150.1, 151.5],
                "High": [155.0, 153.0],
                "Low": [149.0, 150.0],
                "Close": [152.0, 151.8],
                "Volume": [1_000_000, 900_000],
                "Adj Close": [150.5, 151.2],
            },
            index=idx,
        )

    def _multi_ticker_raw(self, tickers: list[str]) -> pd.DataFrame:
        """Simulate yfinance output for multiple tickers (MultiIndex columns)."""
        idx = pd.to_datetime(["2024-01-02"])
        price_cols = ["Open", "High", "Low", "Close", "Adj Close"]
        arrays = [
            [col for col in price_cols for _ in tickers] + ["Volume"] * len(tickers),
            tickers * len(price_cols) + tickers,
        ]
        cols = pd.MultiIndex.from_arrays(arrays)
        # open, high, low, close, adj_close per ticker, then volume per ticker
        row = [150.0, 200.0,  # Open
               155.0, 205.0,  # High
               149.0, 199.0,  # Low
               152.0, 202.0,  # Close
               150.5, 201.0,  # Adj Close
               1_000_000, 500_000]  # Volume
        return pd.DataFrame([row], index=idx, columns=cols)

    def test_single_ticker_columns(self) -> None:
        raw = self._single_ticker_raw()
        result = _normalise_yf_download(raw, ["AAPL"])
        expected_cols = {"ticker", "date", "open", "high", "low", "close", "volume", "source_adj_close"}
        assert expected_cols.issubset(set(result.columns))

    def test_single_ticker_row_count(self) -> None:
        raw = self._single_ticker_raw()
        result = _normalise_yf_download(raw, ["AAPL"])
        assert len(result) == 2

    def test_single_ticker_uses_correct_ticker(self) -> None:
        raw = self._single_ticker_raw()
        result = _normalise_yf_download(raw, ["AAPL"])
        assert (result["ticker"] == "AAPL").all()

    def test_single_ticker_date_is_python_date(self) -> None:
        raw = self._single_ticker_raw()
        result = _normalise_yf_download(raw, ["AAPL"])
        assert isinstance(result["date"].iloc[0], date)

    def test_single_ticker_close_is_decimal(self) -> None:
        raw = self._single_ticker_raw()
        result = _normalise_yf_download(raw, ["AAPL"])
        assert isinstance(result["close"].iloc[0], Decimal)

    def test_multi_ticker_extracts_correct_rows(self) -> None:
        raw = self._multi_ticker_raw(["AAPL", "MSFT"])
        result = _normalise_yf_download(raw, ["AAPL", "MSFT"])
        assert set(result["ticker"].unique()) == {"AAPL", "MSFT"}
        assert len(result) == 2

    def test_empty_raw_returns_empty_with_correct_columns(self) -> None:
        result = _normalise_yf_download(pd.DataFrame(), ["AAPL"])
        assert result.empty
        assert "ticker" in result.columns

    def test_nan_close_rows_dropped(self) -> None:
        raw = self._single_ticker_raw()
        raw.loc[raw.index[0], "Close"] = float("nan")
        result = _normalise_yf_download(raw, ["AAPL"])
        assert len(result) == 1  # NaN close row dropped


# ─── YFinanceClient integration-style tests (mocked) ─────────────────────────

class TestNormaliseYfActions:
    def test_single_ticker_extracts_nonzero_actions(self) -> None:
        raw = pd.DataFrame(
            {
                "Dividends": [0.25, 0.0],
                "Stock Splits": [0.0, 2.0],
            },
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )

        result = _normalise_yf_actions(raw, ["AAPL"])

        assert list(result["ticker"]) == ["AAPL", "AAPL"]
        assert list(result["action_type"]) == ["dividend", "split"]
        assert list(result["value"]) == [Decimal("0.25"), Decimal("2.0")]
        assert all(isinstance(value, date) for value in result["ex_date"])

    def test_multi_ticker_keeps_actions_with_their_ticker(self) -> None:
        columns = pd.MultiIndex.from_tuples(
            [
                ("Dividends", "AAPL"),
                ("Stock Splits", "AAPL"),
                ("Dividends", "MSFT"),
                ("Stock Splits", "MSFT"),
            ]
        )
        raw = pd.DataFrame(
            [[0.25, 0.0, 0.0, 3.0]],
            index=pd.to_datetime(["2024-01-02"]),
            columns=columns,
        )

        result = _normalise_yf_actions(raw, ["AAPL", "MSFT"])

        assert set(zip(result["ticker"], result["action_type"])) == {
            ("AAPL", "dividend"),
            ("MSFT", "split"),
        }

    def test_empty_response_has_writer_compatible_columns(self) -> None:
        result = _normalise_yf_actions(pd.DataFrame(), ["AAPL"])

        assert result.empty
        assert list(result.columns) == [
            "ticker",
            "ex_date",
            "action_type",
            "value",
            "notes",
            "source",
        ]


class TestBackfillFetch:
    @patch("data.ingestion.market.yfinance_client.yf.download")
    def test_combines_prices_and_actions_in_one_sequential_call(
        self, mock_download: MagicMock
    ) -> None:
        mock_download.return_value = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [105.0],
                "Low": [99.0],
                "Close": [102.0],
                "Adj Close": [101.5],
                "Volume": [1_000],
                "Dividends": [0.25],
                "Stock Splits": [0.0],
            },
            index=pd.to_datetime(["2024-01-02"]),
        )

        prices, actions = _yf_fetch(
            ["AAPL"], date(2024, 1, 1), date(2024, 1, 3)
        )

        assert len(prices) == 1
        assert len(actions) == 1
        assert actions.iloc[0]["action_type"] == "dividend"
        mock_download.assert_called_once()
        call_kwargs = mock_download.call_args.kwargs
        assert call_kwargs["actions"] is True
        assert call_kwargs["threads"] is False
        assert call_kwargs["timeout"] == 15


class TestYFinanceClientFetchOhlcv:
    @pytest.fixture
    def client(self) -> YFinanceClient:
        return YFinanceClient(batch_size=3, inter_batch_delay=0)

    def _mock_download(self, tickers: list[str]) -> pd.DataFrame:
        idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
        return pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [105.0, 106.0],
                "Low": [99.0, 100.0],
                "Close": [102.0, 103.0],
                "Volume": [1_000, 2_000],
                "Adj Close": [101.5, 102.5],
            },
            index=idx,
        )

    def test_raises_on_invalid_date_range(self, client: YFinanceClient) -> None:
        with pytest.raises(ValueError, match="start"):
            client.fetch_ohlcv(["AAPL"], start=date(2024, 1, 5), end=date(2024, 1, 1))

    def test_empty_tickers_returns_empty_df(self, client: YFinanceClient) -> None:
        result = client.fetch_ohlcv([], start=date(2024, 1, 1), end=date(2024, 1, 3))
        assert result.empty

    @patch("data.ingestion.market.yfinance_client.yf.download")
    def test_source_column_is_yfinance(self, mock_dl: MagicMock, client: YFinanceClient) -> None:
        mock_dl.return_value = self._mock_download(["AAPL"])
        result = client.fetch_ohlcv(["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 3))
        assert (result["source"] == "yfinance").all()

    @patch("data.ingestion.market.yfinance_client.yf.download")
    def test_batching_calls_download_multiple_times(
        self, mock_dl: MagicMock, client: YFinanceClient
    ) -> None:
        mock_dl.return_value = self._mock_download(["AAPL"])
        # batch_size=3, so 7 tickers = 3 batches
        tickers = [f"T{i}" for i in range(7)]
        client.fetch_ohlcv(tickers, start=date(2024, 1, 1), end=date(2024, 1, 3))
        assert mock_dl.call_count == 3

    @patch("data.ingestion.market.yfinance_client.yf.download")
    def test_download_exception_continues(
        self, mock_dl: MagicMock, client: YFinanceClient
    ) -> None:
        """A failing batch should not crash the run — it logs and continues."""
        mock_dl.side_effect = [RuntimeError("network error"), self._mock_download(["B"])]
        # 2 tickers with batch_size=3 = 1 call; but with batch_size=1 it's 2 calls
        small_client = YFinanceClient(batch_size=1, inter_batch_delay=0)

        with patch("data.ingestion.market.yfinance_client.yf.download") as mock2:
            mock2.side_effect = [RuntimeError("network error"), self._mock_download(["MSFT"])]
            result = small_client.fetch_ohlcv(["AAPL", "MSFT"], start=date(2024, 1, 1), end=date(2024, 1, 3))

        # Should not raise; may return partial or empty results
        assert isinstance(result, pd.DataFrame)


class TestYFinanceClientFetchCorporateActions:
    @pytest.fixture
    def client(self) -> YFinanceClient:
        return YFinanceClient(inter_batch_delay=0)

    def test_empty_tickers_returns_empty(self, client: YFinanceClient) -> None:
        result = client.fetch_corporate_actions([], start=date(2024, 1, 1), end=date(2024, 1, 31))
        assert result.empty

    def test_raises_on_invalid_date_range(self, client: YFinanceClient) -> None:
        with pytest.raises(ValueError, match="start"):
            client.fetch_corporate_actions(
                ["AAPL"], start=date(2024, 2, 1), end=date(2024, 1, 1)
            )

    @patch("data.ingestion.market.yfinance_client.yf.Ticker")
    def test_filters_by_date_range(self, mock_ticker_cls: MagicMock, client: YFinanceClient) -> None:
        mock_ticker = MagicMock()
        mock_ticker.splits = pd.Series(
            {
                pd.Timestamp("2024-01-10"): 2.0,
                pd.Timestamp("2024-02-20"): 3.0,  # outside range
            }
        )
        mock_ticker.dividends = pd.Series(dtype=float)
        mock_ticker_cls.return_value = mock_ticker

        result = client.fetch_corporate_actions(
            ["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 31)
        )

        # Only the Jan 10 split should appear
        assert len(result) == 1
        assert result.iloc[0]["action_type"] == "split"
