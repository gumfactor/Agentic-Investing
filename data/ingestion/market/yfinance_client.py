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

import random
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


def _normalise_yf_actions(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Extract dividends and splits from a yfinance download response."""
    columns = ["ticker", "ex_date", "action_type", "value", "notes", "source"]
    if raw.empty:
        return pd.DataFrame(columns=columns)

    records: list[dict] = []
    for ticker in tickers:
        if isinstance(raw.columns, pd.MultiIndex):
            try:
                sub = raw.xs(ticker, axis=1, level=1)
            except KeyError:
                continue
        else:
            sub = raw

        for field, action_type in (("Dividends", "dividend"), ("Stock Splits", "split")):
            if field not in sub.columns:
                continue
            for ex_dt, value in sub[field].dropna().items():
                decimal_value = _to_decimal(value)
                if decimal_value is None or decimal_value == 0:
                    continue
                records.append(
                    {
                        "ticker": ticker,
                        "ex_date": ex_dt.date() if hasattr(ex_dt, "date") else ex_dt,
                        "action_type": action_type,
                        "value": decimal_value,
                        "notes": None,
                        "source": "yfinance",
                    }
                )

    return pd.DataFrame(records, columns=columns)


class YFinanceClient(BaseMarketDataClient):
    """Market data client backed by yfinance.

    Thread-safe for read operations. Do not share a single instance across
    processes (yfinance is not multiprocess-safe).
    """

    def __init__(self, batch_size: int = _BATCH_SIZE, inter_batch_delay: float = _INTER_BATCH_DELAY) -> None:
        self._batch_size = batch_size
        self._inter_batch_delay = inter_batch_delay

    def fetch_market_data(
        self,
        tickers: list[str],
        start: date,
        end: date,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Fetch OHLCV and corporate actions from the same batched responses."""
        self.validate_date_range(start, end)

        price_frames: list[pd.DataFrame] = []
        action_frames: list[pd.DataFrame] = []
        batches = [
            tickers[i : i + self._batch_size]
            for i in range(0, len(tickers), self._batch_size)
        ]

        for batch_idx, batch in enumerate(batches):
            try:
                raw = yf.download(
                    tickers=batch,
                    start=datetime.combine(start, datetime.min.time()),
                    end=datetime.combine(end, datetime.min.time()) + pd.Timedelta(days=1),
                    auto_adjust=False,
                    actions=True,
                    progress=False,
                    threads=True,
                    timeout=15,
                )
                prices = _normalise_yf_download(raw, batch)
                if not prices.empty:
                    prices["source"] = "yfinance"
                    price_frames.append(prices)

                actions = _normalise_yf_actions(raw, batch)
                if not actions.empty:
                    action_frames.append(actions)
            except Exception as exc:
                logger.error(
                    "fetch_market_data_batch_failed",
                    batch=batch_idx + 1,
                    total_batches=len(batches),
                    error=str(exc),
                )

            if batch_idx < len(batches) - 1:
                time.sleep(self._inter_batch_delay)

        price_columns = [
            "ticker",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source_adj_close",
            "source",
        ]
        action_columns = ["ticker", "ex_date", "action_type", "value", "notes", "source"]
        prices = (
            pd.concat(price_frames, ignore_index=True)
            if price_frames
            else pd.DataFrame(columns=price_columns)
        )
        actions = (
            pd.concat(action_frames, ignore_index=True)
            if action_frames
            else pd.DataFrame(columns=action_columns)
        )
        return prices, actions

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

# Tuned for Yahoo Finance's unofficial rate limits.  Smaller batches and longer
# delays prevent the 429s that kill a 500-ticker × 5-year run.
_BACKFILL_BATCH_SIZE = 20          # tickers per yf.download call
_BACKFILL_INTER_BATCH_DELAY = 3.0  # seconds between batches
_BACKFILL_RETRY_DELAYS = (5, 10, 20)  # seconds; one entry per attempt after first
_BACKFILL_SINGLE_TICKER_DELAY = 2.0  # seconds between per-ticker fallback retries
_BACKFILL_JITTER_MAX = 2.0  # random seconds added to avoid a fixed request cadence
_BACKFILL_REQUEST_TIMEOUT = 15  # seconds per Yahoo request


def _backfill_sleep(base_seconds: float) -> None:
    """Sleep with jitter so repeated requests do not follow a fixed cadence."""
    time.sleep(base_seconds + random.uniform(0, _BACKFILL_JITTER_MAX))


def _yf_fetch(
    tickers: list[str], start_date: date, end_date: date
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download OHLCV and corporate actions together; the caller handles retries.

    Combining data types avoids a second full-universe pass over Yahoo.
    yfinance may silently drop individual tickers from the result when Yahoo
    returns an empty response for them ('Expecting value: line 1 column 1');
    callers should check which tickers are actually present in the result.
    """
    raw = yf.download(
        tickers=tickers,
        start=datetime.combine(start_date, datetime.min.time()),
        end=datetime.combine(end_date, datetime.min.time()) + pd.Timedelta(days=1),
        auto_adjust=False,
        actions=True,
        progress=False,
        threads=False,
        timeout=_BACKFILL_REQUEST_TIMEOUT,
    )
    df = _normalise_yf_download(raw, tickers)
    if not df.empty:
        df["source"] = "yfinance"
    return df, _normalise_yf_actions(raw, tickers)


def _backfill_cli() -> None:
    """Backfill 5 years of daily OHLCV for the configured universe.

    Invoked via: python -m data.ingestion.market.yfinance_client backfill
    or:          make backfill

    Design for scale
    ----------------
    * 20-ticker batches → stays well under Yahoo's informal rate limit.
    * Resumable: tickers already in daily_prices (near the start date) are
      skipped, so an interrupted run continues rather than re-fetching.
    * Incremental writes: upserts after every batch, so a crash only loses
      the current batch (≤20 tickers).
    * OHLCV and corporate actions share each response, avoiding a second pass.
    * Per-batch retry: up to 3 attempts with jittered 5 s / 10 s / 20 s back-off.
    * Quality flags written per batch so partial results are still flagged.

    Imports are deferred to avoid circular dependencies when this module
    is used as a library.
    """
    from datetime import timedelta
    from dotenv import load_dotenv

    load_dotenv()  # must precede any import that reads os.environ

    from data.normalization.quality_checks import run_quality_checks
    from data.storage.timescale_writer import TimescaleWriter

    # OPERATIONAL CURRENT-UNIVERSE MODE (BUG-008 / 01B-2): this backfill
    # decides which tickers to fetch price history for, using current S&P 500
    # membership. The price ROWS it writes are usable by historical research,
    # but historical ELIGIBILITY must always come from
    # data.universe.runtime.PITUniverseLookup — tickers fetched here that were
    # not members on a given historical date are excluded by that layer, and
    # removed constituents this loader never fetches are precisely the
    # survivorship gap the PIT universe contract documents (a commercial
    # delisted-price source closes it at Gate 03).
    from config.universe_loader import load_universe

    end_date = date.today()
    start_date = end_date - timedelta(days=5 * 365)

    tickers = load_universe()
    writer = TimescaleWriter()

    # ── Resumability ──────────────────────────────────────────────────────────
    already_done = writer.get_tickers_with_data(start_date, end_date)
    pending = [t for t in tickers if t not in already_done]

    logger.info(
        "backfill_start",
        total_tickers=len(tickers),
        already_done=len(already_done),
        pending=len(pending),
        start=str(start_date),
        end=str(end_date),
    )

    if not pending:
        logger.info("backfill_already_complete")
        return

    # ── OHLCV — batched, retried, incremental ────────────────────────────────
    batches = [
        pending[i : i + _BACKFILL_BATCH_SIZE]
        for i in range(0, len(pending), _BACKFILL_BATCH_SIZE)
    ]
    total_ohlcv_written = 0
    total_actions_written = 0
    failed_tickers: list[str] = []

    for batch_idx, batch in enumerate(batches):
        batch_log = logger.bind(
            batch=batch_idx + 1,
            total_batches=len(batches),
            n_tickers=len(batch),
        )

        # Retry loop — first attempt + len(_BACKFILL_RETRY_DELAYS) retries
        batch_df: pd.DataFrame | None = None
        batch_actions = pd.DataFrame()
        all_delays = (0,) + _BACKFILL_RETRY_DELAYS
        for attempt, pre_sleep in enumerate(all_delays):
            if pre_sleep:
                batch_log.warning(
                    "backfill_batch_retry",
                    attempt=attempt,
                    wait_s=pre_sleep,
                )
                _backfill_sleep(pre_sleep)
            try:
                batch_df, batch_actions = _yf_fetch(batch, start_date, end_date)
                if batch_df.empty:
                    raise RuntimeError("Yahoo returned no price data")
                break
            except Exception as exc:
                batch_df = None
                batch_log.error(
                    "backfill_batch_attempt_failed",
                    attempt=attempt + 1,
                    error=str(exc),
                )
                if attempt == len(all_delays) - 1:
                    failed_tickers.extend(batch)

        if batch_df is None or batch_df.empty:
            batch_log.warning("backfill_batch_no_data")
        else:
            # yfinance silently drops tickers that Yahoo returns empty for
            # ('Expecting value: line 1 column 1').  Detect them and retry
            # individually so a single throttled ticker doesn't lose the batch.
            returned = set(batch_df["ticker"].unique())
            missing = [t for t in batch if t not in returned]
            if missing:
                batch_log.warning("backfill_tickers_missing_from_batch", missing=missing)
                recovered: list[pd.DataFrame] = [batch_df]
                recovered_actions: list[pd.DataFrame] = [batch_actions]
                for miss in missing:
                    _backfill_sleep(_BACKFILL_SINGLE_TICKER_DELAY)
                    try:
                        single_df, single_actions = _yf_fetch([miss], start_date, end_date)
                        if not single_df.empty:
                            recovered.append(single_df)
                            if not single_actions.empty:
                                recovered_actions.append(single_actions)
                            batch_log.info("backfill_ticker_recovered", ticker=miss)
                        else:
                            failed_tickers.append(miss)
                            batch_log.warning("backfill_ticker_permanently_empty", ticker=miss)
                    except Exception as exc:
                        failed_tickers.append(miss)
                        batch_log.error("backfill_ticker_failed", ticker=miss, error=str(exc))
                batch_df = pd.concat(recovered, ignore_index=True)
                batch_actions = pd.concat(recovered_actions, ignore_index=True)

            # Quality checks per batch
            flags = run_quality_checks(batch_df)
            if not flags.empty:
                error_count = int((flags["severity"] == "error").sum())
                batch_log.warning(
                    "quality_flags_detected", total=len(flags), errors=error_count
                )
                writer.write_quality_flags(flags)

            # Incremental write — crash loses at most this batch
            written = writer.upsert_ohlcv(batch_df)
            total_ohlcv_written += written
            if not batch_actions.empty:
                total_actions_written += writer.upsert_corporate_actions(batch_actions)
            batch_log.info(
                "backfill_batch_complete",
                rows=len(batch_df),
                records_written=written,
                corporate_actions_written=len(batch_actions),
                cumulative_written=total_ohlcv_written,
            )

        if batch_idx < len(batches) - 1:
            _backfill_sleep(_BACKFILL_INTER_BATCH_DELAY)

    logger.info(
        "backfill_ohlcv_complete",
        total_records_written=total_ohlcv_written,
        total_corporate_actions_written=total_actions_written,
        failed_ticker_count=len(failed_tickers),
    )
    if failed_tickers:
        logger.warning("backfill_failed_tickers", tickers=failed_tickers)

    logger.info("backfill_complete")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        _backfill_cli()
