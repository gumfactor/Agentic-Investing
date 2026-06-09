"""DataFrame-based completeness checks for ingested market data.

These checks operate on data already loaded by the caller — no database
connection is required. They complement quality_checks.py, which focuses
on per-bar validity (negative prices, HLOC violations, etc.). These checks
focus on structural completeness: duplicates, nulls, short histories, and
cross-ticker coverage gaps.

All public functions return ``list[dict]`` with keys:
    ticker, date, flag_type, severity, message

The top-level ``run_completeness_checks`` collects all flags into a single
DataFrame with those columns (empty DataFrame if no issues).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# ─── Required columns ─────────────────────────────────────────────────────────

_REQUIRED_COLUMNS: frozenset[str] = frozenset({"ticker", "date", "close"})


def _validate_columns(df: pd.DataFrame) -> None:
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"Input DataFrame is missing required columns: {sorted(missing)}. "
            f"Expected at least: {sorted(_REQUIRED_COLUMNS)}."
        )


# ─── Public API ───────────────────────────────────────────────────────────────

def run_completeness_checks(df: pd.DataFrame) -> pd.DataFrame:
    """Run all completeness checks on a batch of market data rows.

    Args:
        df: DataFrame with at minimum columns ``ticker``, ``date``, ``close``.

    Returns:
        DataFrame of flag records with columns:
            ticker, date, flag_type, severity, message
        Empty DataFrame (with those columns) if no issues found.

    Raises:
        ValueError: If ``df`` is missing any of the required columns.
    """
    _validate_columns(df)

    if df.empty:
        return pd.DataFrame(columns=["ticker", "date", "flag_type", "severity", "message"])

    flags: list[dict[str, Any]] = []
    flags.extend(check_duplicates(df))
    flags.extend(check_null_prices(df))
    flags.extend(check_short_histories(df))
    flags.extend(check_coverage(df))

    result = pd.DataFrame(flags, columns=["ticker", "date", "flag_type", "severity", "message"])

    logger.info(
        "completeness_check_complete",
        total_rows=len(df),
        total_tickers=df["ticker"].nunique(),
        total_flags=len(result),
        errors=int((result["severity"] == "error").sum()) if not result.empty else 0,
        warnings=int((result["severity"] == "warning").sum()) if not result.empty else 0,
    )
    return result


def check_duplicates(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Find rows with duplicate (ticker, date) pairs.

    The first occurrence is not flagged; every subsequent occurrence is.

    Args:
        df: DataFrame with columns ``ticker`` and ``date``.

    Returns:
        List of flag dicts (may be empty). severity=error.
    """
    flags: list[dict[str, Any]] = []

    duplicated_mask = df.duplicated(subset=["ticker", "date"], keep="first")
    for _, row in df[duplicated_mask].iterrows():
        flags.append(
            {
                "ticker": row["ticker"],
                "date": row["date"],
                "flag_type": "duplicate_row",
                "severity": "error",
                "message": (
                    f"Duplicate (ticker, date) pair: ({row['ticker']}, {row['date']})."
                ),
            }
        )
    return flags


def check_null_prices(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Find rows where ``close`` is None or NaN.

    Args:
        df: DataFrame with columns ``ticker``, ``date``, and ``close``.

    Returns:
        List of flag dicts (may be empty). severity=error.
    """
    flags: list[dict[str, Any]] = []

    null_mask = df["close"].isna()
    for _, row in df[null_mask].iterrows():
        flags.append(
            {
                "ticker": row["ticker"],
                "date": row["date"],
                "flag_type": "null_price",
                "severity": "error",
                "message": f"close is null/NaN for {row['ticker']} on {row['date']}.",
            }
        )
    return flags


def check_short_histories(
    df: pd.DataFrame,
    min_rows: int = 252,
) -> list[dict[str, Any]]:
    """Flag tickers whose total row count falls below ``min_rows``.

    The flag's ``date`` field is set to the ticker's earliest date in the
    DataFrame so that the flag is anchored to a concrete calendar point.

    Args:
        df: DataFrame with columns ``ticker`` and ``date``.
        min_rows: Minimum number of rows required. Defaults to 252 (≈ 1
            trading year).

    Returns:
        List of flag dicts (may be empty). severity=warning.
    """
    flags: list[dict[str, Any]] = []

    ticker_counts = df.groupby("ticker")["date"].agg(["count", "min"])
    short = ticker_counts[ticker_counts["count"] < min_rows]

    for ticker, row_data in short.iterrows():
        flags.append(
            {
                "ticker": ticker,
                "date": row_data["min"],
                "flag_type": "short_history",
                "severity": "warning",
                "message": (
                    f"Ticker {ticker} has only {row_data['count']} rows "
                    f"(minimum required: {min_rows})."
                ),
            }
        )
    return flags


def check_coverage(
    df: pd.DataFrame,
    coverage_threshold: float = 0.7,
) -> list[dict[str, Any]]:
    """Flag tickers whose distinct date count is below a fraction of the reference.

    The reference count is the maximum number of distinct dates held by any
    single ticker in the DataFrame.  Tickers with fewer than
    ``coverage_threshold * reference_count`` distinct dates are flagged.

    Args:
        df: DataFrame with columns ``ticker`` and ``date``.
        coverage_threshold: Fraction of the reference count below which a
            ticker is flagged. Defaults to 0.70 (70%).

    Returns:
        List of flag dicts (may be empty). severity=warning.
    """
    flags: list[dict[str, Any]] = []

    ticker_date_counts = df.groupby("ticker")["date"].nunique()
    if ticker_date_counts.empty:
        return flags

    reference_count = int(ticker_date_counts.max())
    min_required = coverage_threshold * reference_count

    ticker_min_dates = df.groupby("ticker")["date"].min()

    below_threshold = ticker_date_counts[ticker_date_counts < min_required]
    for ticker, distinct_count in below_threshold.items():
        flags.append(
            {
                "ticker": ticker,
                "date": ticker_min_dates[ticker],
                "flag_type": "low_coverage",
                "severity": "warning",
                "message": (
                    f"Ticker {ticker} has {distinct_count} distinct dates "
                    f"({distinct_count / reference_count:.1%} of reference "
                    f"count {reference_count}); "
                    f"threshold is {coverage_threshold:.0%}."
                ),
            }
        )
    return flags
