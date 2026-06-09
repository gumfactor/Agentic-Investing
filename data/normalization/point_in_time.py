"""Point-in-time (PIT) data access utilities.

CRITICAL: These utilities are the primary defense against look-ahead bias.

Look-ahead bias occurs when a backtest or signal computation uses data that
would not have been available on the simulation date. For example:
  - Using Q3 earnings that were announced in November to make a trade in October.
  - Using a price 'close' from Tuesday to make a trade at Tuesday's open.

Every data access in backtesting MUST go through pit_join or an equivalent
PIT-aware accessor. Direct DataFrame slicing by date (e.g., df[df['date'] <= d])
is NEVER sufficient — it must also filter on release_date.

Release date conventions used in this system:
  - OHLCV bars       : release_date = date itself (available after market close that day).
                        For intraday, release_date = bar timestamp.
  - Corporate actions: release_date = ex_date (public on that date).
  - Earnings (Phase 2): release_date = actual filing/announcement date,
                        which is typically 30–90 days after period_end_date.
  - SEC filings (Phase 2): release_date = filing date.

The distinction between date and release_date matters most for fundamentals.
For OHLCV, date == release_date, so the check is trivially satisfied.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)


def pit_join(
    df: pd.DataFrame,
    as_of_date: date,
    date_col: str = "date",
    release_date_col: str | None = None,
) -> pd.DataFrame:
    """Return rows visible as of as_of_date, respecting point-in-time correctness.

    For OHLCV data where date == release_date, set release_date_col=None (default).
    For fundamentals or alternative data with a separate release_date column,
    pass release_date_col='release_date' (or the column name used in that table).

    Args:
        df           : Input DataFrame.
        as_of_date   : The simulation date. Only data visible by end-of-day
                       on this date will be returned.
        date_col     : Column containing the data's own date/period.
        release_date_col : Column containing when the data became public.
                           If None, date_col is used as the release date.

    Returns:
        Filtered DataFrame containing only rows where:
          release_date <= as_of_date  AND  date <= as_of_date

    Raises:
        KeyError: if date_col or release_date_col is not found in df.
    """
    if df.empty:
        return df.copy()

    if date_col not in df.columns:
        raise KeyError(f"date_col '{date_col}' not found in DataFrame columns: {list(df.columns)}")

    release_col = release_date_col if release_date_col is not None else date_col

    if release_col not in df.columns:
        raise KeyError(
            f"release_date_col '{release_col}' not found in DataFrame columns: {list(df.columns)}"
        )

    date_mask = df[date_col] <= as_of_date
    release_mask = df[release_col] <= as_of_date

    result = df[date_mask & release_mask].copy()

    logger.debug(
        "pit_join",
        as_of_date=str(as_of_date),
        input_rows=len(df),
        output_rows=len(result),
        rows_excluded=len(df) - len(result),
    )

    return result


def pit_latest(
    df: pd.DataFrame,
    as_of_date: date,
    group_cols: list[str],
    date_col: str = "date",
    release_date_col: str | None = None,
) -> pd.DataFrame:
    """Return the most-recent row per group visible as of as_of_date.

    Useful for fundamentals: given a full history of quarterly earnings,
    return the most-recent quarter available as of a given simulation date.

    Args:
        df           : Input DataFrame (full history).
        as_of_date   : Simulation date.
        group_cols   : Columns to group by (e.g., ['ticker']).
        date_col     : Period date column.
        release_date_col : Release date column (if separate from date_col).

    Returns:
        DataFrame with one row per group containing the latest available record.
    """
    visible = pit_join(df, as_of_date, date_col=date_col, release_date_col=release_date_col)

    if visible.empty:
        return visible

    release_col = release_date_col if release_date_col is not None else date_col
    return visible.sort_values(release_col).groupby(group_cols).last().reset_index()


def add_ohlcv_release_date(df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    """Add a release_date column to an OHLCV DataFrame.

    For daily bars: release_date == date (bar is available after market close
    on the same calendar day). This is the conservative assumption — some
    providers deliver end-of-day data the following morning. For a daily-
    rebalancing strategy, use date + 1 business day if that matches your
    actual data delivery time.

    This function keeps the OHLCV contract explicit rather than relying on
    callers to know that date == release_date for market data.
    """
    df = df.copy()
    df["release_date"] = df[date_col]
    return df
