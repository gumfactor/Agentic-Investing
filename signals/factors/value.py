"""Value factor: earnings yield, book-to-market, FCF yield.

All three ratios use market capitalisation (price × shares_outstanding) as
the denominator.  Prices come from daily_prices; fundamentals from
financial_statements via pit_latest().

Point-in-time correctness
--------------------------
Fundamental values are forward-joined to price dates using pit_latest():
for each score_date, the most recently *filed* quarterly value is used
(filing date <= score_date).  This prevents look-ahead bias from using
earnings that hadn't been reported yet on a given date.

Survivorship bias note
----------------------
Same as momentum: current-membership S&P 500 universe in Phase 1.
Results labelled provisional until PIT constituent history is in place.

Output sign convention
-----------------------
All three factors are defined so that HIGHER score = BETTER:
  earnings_yield  : high E/P → high score (cheap stock)
  book_to_market  : high B/P → high score (cheap stock)
  fcf_yield       : high FCF/P → high score (cash-generative)
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_REQUIRED_FUND_COLS = {"ticker", "period_end_date", "release_date", "period_type", "item_name", "value"}
_REQUIRED_PRICE_COLS = {"ticker", "date", "close"}


def compute_value_scores(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
    score_dates: Optional[list] = None,
    min_tickers: int = 10,
) -> pd.DataFrame:
    """Compute cross-sectional value scores at each score date.

    Args:
        fundamentals: Long-format financial_statements rows.  Must include
            items 'net_income', 'total_equity', 'free_cash_flow',
            'shares_outstanding'.  Period-type filtering (quarterly/annual)
            is the caller's responsibility.
        prices: Long-format daily_prices rows (ticker, date, close).
        score_dates: Dates to compute scores for.  Defaults to all dates
            present in prices.
        min_tickers: Minimum number of tickers with valid fundamental data
            required to compute scores for a date.  Dates below this are
            dropped.

    Returns:
        Long-format DataFrame with columns:
            ticker, date,
            earnings_yield, book_to_market, fcf_yield,
            value_score  (equal-weight composite of available sub-scores)

        Returns empty DataFrame if fundamentals or prices are empty.
    """
    _validate_fundamentals(fundamentals)
    _validate_prices(prices)

    if score_dates is None:
        score_dates = sorted(prices["date"].unique())

    rows: list[dict] = []

    for score_date in score_dates:
        # Point-in-time fundamentals: most recently filed value per (ticker, item)
        # with release_date <= score_date.
        pit_fund = _pit_latest_fundamentals(fundamentals, score_date)
        if not pit_fund:
            continue

        # Prices on score_date
        price_snap = (
            prices[prices["date"] == score_date][["ticker", "close"]]
            .assign(close=lambda df: df["close"].astype(float))
            .set_index("ticker")
        )
        if price_snap.empty:
            continue

        # Market cap requires shares_outstanding
        shares = pit_fund.get("shares_outstanding")
        if shares is None:
            mcap = None
        else:
            mcap = price_snap["close"].mul(shares.reindex(price_snap.index)).dropna()

        date_rows = _compute_ratios(pit_fund, mcap, score_date)
        if len(date_rows) < min_tickers:
            continue
        rows.extend(date_rows)

    if not rows:
        return pd.DataFrame(
            columns=["ticker", "date", "earnings_yield", "book_to_market", "fcf_yield", "value_score"]
        )

    df = pd.DataFrame(rows)
    sub_cols = [c for c in ["earnings_yield", "book_to_market", "fcf_yield"] if c in df.columns]

    # Cross-sectional z-score per date, per sub-factor
    for col in sub_cols:
        df[col] = df.groupby("date")[col].transform(_zscore)

    df["value_score"] = df[sub_cols].mean(axis=1, skipna=True)
    df = df.dropna(subset=sub_cols, how="all")
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "value_scores_computed",
        dates=df["date"].nunique(),
        tickers=df["ticker"].nunique(),
        sub_factors=sub_cols,
    )
    return df


# ── Internal helpers ───────────────────────────────────────────────────────────

def _pit_latest_fundamentals(
    fundamentals: pd.DataFrame,
    as_of_date,
) -> dict[str, pd.Series]:
    """Return a dict of item_name → (ticker-indexed) Series, PIT-filtered.

    Uses only rows where release_date <= as_of_date, then takes the
    most-recently-filed observation per (ticker, item_name).
    """
    visible = fundamentals[fundamentals["release_date"] <= as_of_date]
    if visible.empty:
        return {}

    latest = (
        visible.sort_values("release_date")
        .groupby(["ticker", "item_name"])
        .last()
        .reset_index()[["ticker", "item_name", "value"]]
    )

    result: dict[str, pd.Series] = {}
    for item, group in latest.groupby("item_name"):
        series = group.set_index("ticker")["value"].astype(float)
        series.name = item
        result[item] = series

    return result


def _compute_ratios(
    pit_fund: dict[str, pd.Series],
    mcap: Optional[pd.Series],
    score_date,
) -> list[dict]:
    """Compute per-ticker ratios for a single score date."""
    rows: list[dict] = []

    net_income = pit_fund.get("net_income")
    total_equity = pit_fund.get("total_equity")
    fcf = pit_fund.get("free_cash_flow")

    if mcap is None or len(mcap) == 0:
        # Without market cap we can't compute yield-based factors
        return rows

    common_index = mcap.index

    for ticker in common_index:
        row: dict = {"ticker": ticker, "date": score_date}

        mc = mcap.get(ticker)
        if mc is None or mc <= 0:
            continue

        if net_income is not None and ticker in net_income.index:
            ni = float(net_income[ticker])
            row["earnings_yield"] = ni / mc

        if total_equity is not None and ticker in total_equity.index:
            eq = float(total_equity[ticker])
            row["book_to_market"] = eq / mc

        if fcf is not None and ticker in fcf.index:
            cf = float(fcf[ticker])
            row["fcf_yield"] = cf / mc

        if len(row) > 2:  # more than just ticker + date
            rows.append(row)

    return rows


def _zscore(series: pd.Series) -> pd.Series:
    """Cross-sectional z-score (within the passed series)."""
    std = series.std(ddof=1)
    if std == 0 or np.isnan(std):
        return series * np.nan
    return (series - series.mean()) / std


def _validate_fundamentals(fundamentals: pd.DataFrame) -> None:
    missing = _REQUIRED_FUND_COLS - set(fundamentals.columns)
    if missing:
        raise ValueError(f"fundamentals DataFrame missing columns: {missing}")


def _validate_prices(prices: pd.DataFrame) -> None:
    missing = _REQUIRED_PRICE_COLS - set(prices.columns)
    if missing:
        raise ValueError(f"prices DataFrame missing columns: {missing}")
