"""yfinance-backed market data client.

Phase 1 data source. Uses Yahoo Finance's unofficial API via the yfinance library.
No API key required. No SLA — treat failures as soft errors and retry.

Swap to Polygon.io in Phase 2 by creating a PolygonClient that implements
BaseMarketDataClient; no change required in consuming code.

NOTE: yfinance returns adjusted prices via a separate 'Adj Close' column
(auto_adjust=False mode, which we use). We store both the unadjusted OHLCV
and the source-provided adjusted close for cross-validation. The authoritative
adjusted prices used in signal computation are derived from corporate_actions
by data/normalization/corporate_actions.py.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import io
import json

import pandas as pd
import structlog
import yfinance as yf

from data.ingestion.market.base_client import BaseMarketDataClient

logger = structlog.get_logger(__name__)

# yfinance handles multi-ticker downloads efficiently; 200 is a safe batch ceiling.
_BATCH_SIZE = 200
# Conservative inter-batch pause to avoid hitting Yahoo rate limits.
_INTER_BATCH_DELAY = 1.0  # seconds


def _to_decimal(value: object) -> Optional[Decimal]:
    """Convert a numeric value to Decimal, returning None for NaN/None."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        # pandas NaN shows up as a very large or 'nan' Decimal
        if d.is_nan() or d.is_infinite():
            return None
        return d
    except InvalidOperation:
        return None


def _normalise_yf_download(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Convert yfinance download output to long-format OHLCV DataFrame.

    yfinance returns:
      - Single ticker : flat columns ['Open', 'High', 'Low', 'Close', 'Volume', 'Adj Close']
      - Multi ticker  : MultiIndex columns [('Open', 'AAPL'), ('Open', 'MSFT'), ...]

    We normalise both to a long DataFrame with columns:
        ticker, date, open, high, low, close, volume, source_adj_close
    """
    if raw.empty:
        return pd.DataFrame(
            columns=["ticker", "date", "open", "high", "low", "close", "volume", "source_adj_close"]
        )

    if isinstance(raw.columns, pd.MultiIndex):
        frames = []
        for ticker in tickers:
            try:
                sub = raw.xs(ticker, axis=1, level=1).copy()
            except KeyError:
                logger.warning("ticker_not_in_yf_response", ticker=ticker)
                continue
            sub = sub.rename(
                columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                    "Adj Close": "source_adj_close",
                }
            )
            sub["ticker"] = ticker
            frames.append(sub)
        if not frames:
            return pd.DataFrame(
                columns=["ticker", "date", "open", "high", "low", "close", "volume", "source_adj_close"]
            )
        df = pd.concat(frames)
    else:
        # Single ticker — raw index is DatetimeIndex
        df = raw.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
                "Adj Close": "source_adj_close",
            }
        )
        df["ticker"] = tickers[0]

    df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Drop rows where close is NaN (delisted or no trading that day)
    df = df.dropna(subset=["close"])

    # Apply Decimal conversion for prices; volume stays int-compatible
    for col in ["open", "high", "low", "close", "source_adj_close"]:
        if col in df.columns:
            df[col] = df[col].apply(_to_decimal)

    df["volume"] = df["volume"].where(df["volume"].notna(), other=None).apply(
        lambda v: int(v) if v is not None else None
    )

    return df[["ticker", "date", "open", "high", "low", "close", "volume", "source_adj_close"]]


class YFinanceClient(BaseMarketDataClient):
    """Market data client backed by yfinance.

    Thread-safe for read operations. Do not share a single instance across
    processes (yfinance is not multiprocess-safe).
    """

    def __init__(self, batch_size: int = _BATCH_SIZE, inter_batch_delay: float = _INTER_BATCH_DELAY) -> None:
        self._batch_size = batch_size
        self._inter_batch_delay = inter_batch_delay

    def fetch_ohlcv(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch daily OHLCV bars for tickers between start (inclusive) and end (inclusive).

        Processes tickers in batches of _batch_size to stay within informal
        Yahoo Finance rate limits. Returns a long-format DataFrame.
        """
        self.validate_date_range(start, end)

        if not tickers:
            return pd.DataFrame(
                columns=["ticker", "date", "open", "high", "low", "close", "volume", "source_adj_close", "source"]
            )

        log = logger.bind(source="yfinance", start=str(start), end=str(end), n_tickers=len(tickers))
        log.info("fetch_ohlcv_start")

        all_frames: list[pd.DataFrame] = []
        batches = [tickers[i : i + self._batch_size] for i in range(0, len(tickers), self._batch_size)]

        for batch_idx, batch in enumerate(batches):
            batch_log = log.bind(batch=batch_idx + 1, total_batches=len(batches), n=len(batch))
            try:
                raw = yf.download(
                    tickers=batch,
                    start=datetime.combine(start, datetime.min.time()),
                    # yfinance end date is exclusive — add one day
                    end=datetime.combine(end, datetime.min.time()) + pd.Timedelta(days=1),
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    threads=True,
                )
                df = _normalise_yf_download(raw, batch)
                df["source"] = "yfinance"
                all_frames.append(df)
                batch_log.info("fetch_ohlcv_batch_complete", rows=len(df))
            except Exception as exc:
                # Log and continue — a single batch failure should not abort the run.
                # The ingestion log will record missing tickers, and the daily pipeline
                # will retry on the next scheduled run.
                batch_log.error("fetch_ohlcv_batch_failed", error=str(exc))

            if batch_idx < len(batches) - 1:
                time.sleep(self._inter_batch_delay)

        if not all_frames:
            log.warning("fetch_ohlcv_no_data_returned")
            return pd.DataFrame(
                columns=["ticker", "date", "open", "high", "low", "close", "volume", "source_adj_close", "source"]
            )

        result = pd.concat(all_frames, ignore_index=True)
        log.info("fetch_ohlcv_complete", total_rows=len(result))
        return result

    def fetch_corporate_actions(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch splits and dividends for tickers over a date range.

        yfinance provides these via Ticker.splits and Ticker.dividends.
        Spinoffs are not available from yfinance — those require a paid source.
        """
        self.validate_date_range(start, end)

        if not tickers:
            return pd.DataFrame(
                columns=["ticker", "ex_date", "action_type", "value", "notes", "source"]
            )

        log = logger.bind(source="yfinance", start=str(start), end=str(end), n_tickers=len(tickers))
        log.info("fetch_corporate_actions_start")

        records: list[dict] = []

        for ticker_sym in tickers:
            try:
                t = yf.Ticker(ticker_sym)

                splits = t.splits
                if splits is not None and not splits.empty:
                    for ex_dt, ratio in splits.items():
                        dt = ex_dt.date() if hasattr(ex_dt, "date") else ex_dt
                        if start <= dt <= end:
                            records.append(
                                {
                                    "ticker": ticker_sym,
                                    "ex_date": dt,
                                    "action_type": "split",
                                    "value": _to_decimal(ratio),
                                    "notes": None,
                                    "source": "yfinance",
                                }
                            )

                divs = t.dividends
                if divs is not None and not divs.empty:
                    for ex_dt, amount in divs.items():
                        dt = ex_dt.date() if hasattr(ex_dt, "date") else ex_dt
                        if start <= dt <= end:
                            records.append(
                                {
                                    "ticker": ticker_sym,
                                    "ex_date": dt,
                                    "action_type": "dividend",
                                    "value": _to_decimal(amount),
                                    "notes": None,
                                    "source": "yfinance",
                                }
                            )
            except Exception as exc:
                log.warning("fetch_corporate_actions_ticker_failed", ticker=ticker_sym, error=str(exc))

        if not records:
            return pd.DataFrame(
                columns=["ticker", "ex_date", "action_type", "value", "notes", "source"]
            )

        df = pd.DataFrame(records)
        log.info("fetch_corporate_actions_complete", total_rows=len(df))
        return df


# ─── CLI entry point (make backfill) ─────────────────────────────────────────

def _backfill_cli() -> None:
    """Backfill 5 years of daily OHLCV for the configured universe.

    Invoked via: python -m data.ingestion.market.yfinance_client backfill
    or:          make backfill

    Imports are deferred to avoid circular dependencies when this module
    is used as a library.
    """
    import sys
    from datetime import timedelta
    from dotenv import load_dotenv

    # Load .env so DATABASE_URL and other env vars are available without
    # the caller having to manually export them first.
    load_dotenv()

    from data.normalization.quality_checks import run_quality_checks
    from data.storage.timescale_writer import TimescaleWriter
    from config.universe_loader import load_universe

    end_date = date.today()
    start_date = end_date - timedelta(days=5 * 365)

    tickers = load_universe()
    logger.info("backfill_start", tickers=len(tickers), start=str(start_date), end=str(end_date))

    client = YFinanceClient()
    writer = TimescaleWriter()

    ohlcv = client.fetch_ohlcv(tickers, start_date, end_date)
    flags = run_quality_checks(ohlcv)

    if not flags.empty:
        error_count = (flags["severity"] == "error").sum()
        logger.warning("quality_flags_detected", total=len(flags), errors=int(error_count))

    records_written = writer.upsert_ohlcv(ohlcv)
    logger.info("backfill_complete", records_written=records_written)

    corp_actions = client.fetch_corporate_actions(tickers, start_date, end_date)
    ca_written = writer.upsert_corporate_actions(corp_actions)
    logger.info("corporate_actions_written", records_written=ca_written)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        _backfill_cli()
