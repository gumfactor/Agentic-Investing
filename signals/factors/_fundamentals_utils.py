"""Shared helpers for fundamental-data factor modules.

Fundamentals DataFrame format (long, semi-wide):
    date    — publication date (NOT period-end date; use the date the data
               became publicly available to avoid look-ahead bias)
    ticker  — equity identifier
    <metric columns> — e.g. eps_ttm, book_value_per_share, revenue_ttm, ...

Typical cadence: quarterly entries per ticker.  align_fundamentals()
forward-fills report-date values to daily price dates so factor functions
can work on the same time axis as prices.
"""
from __future__ import annotations

import pandas as pd


def validate_fundamentals(fundamentals: pd.DataFrame, required_metrics: set[str]) -> None:
    base = {"date", "ticker"}
    missing_base = base - set(fundamentals.columns)
    if missing_base:
        raise ValueError(f"fundamentals DataFrame missing base columns: {missing_base}")
    missing = required_metrics - set(fundamentals.columns)
    if missing:
        raise ValueError(f"fundamentals DataFrame missing required metric columns: {missing}")
    if fundamentals.empty:
        raise ValueError("fundamentals DataFrame is empty")


def fund_to_wide(fundamentals: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Pivot one fundamental metric to wide format (index=report_date, columns=ticker)."""
    wide = (
        fundamentals[["date", "ticker", metric]]
        .assign(**{metric: lambda df: df[metric].astype(float)})
        .pivot_table(index="date", columns="ticker", values=metric)
        .sort_index()
    )
    wide.columns.name = None
    return wide


def align_fundamentals(fund_wide: pd.DataFrame, price_index: pd.Index) -> pd.DataFrame:
    """Forward-fill quarterly fundamentals to align with a daily price index.

    Uses union→sort→ffill→reindex so that every trading date gets the most
    recently published value.  No future data leaks in as long as the report
    dates in fund_wide are publication dates, not period-end dates.
    """
    combined = price_index.union(fund_wide.index)
    return (
        fund_wide
        .reindex(combined)
        .sort_index()
        .ffill()
        .reindex(price_index)
    )


def compute_ev_wide(price_wide: pd.DataFrame, fundamentals: pd.DataFrame) -> pd.DataFrame:
    """Enterprise Value = Market Cap + Total Debt − Cash & Equivalents.

    Requires fundamentals columns: shares_outstanding, total_debt, cash.
    Returns a wide DataFrame aligned to price_wide's index.
    """
    idx = price_wide.index
    shares = align_fundamentals(fund_to_wide(fundamentals, "shares_outstanding"), idx)
    debt   = align_fundamentals(fund_to_wide(fundamentals, "total_debt"), idx)
    cash   = align_fundamentals(fund_to_wide(fundamentals, "cash"), idx)
    mkt_cap = price_wide * shares
    return mkt_cap + debt - cash
