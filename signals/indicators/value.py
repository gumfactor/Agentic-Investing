"""Value factor: earnings yield, book-to-market, FCF yield.

All three ratios use market capitalisation (price × shares_outstanding) as
the denominator.  Prices come from daily_prices; fundamentals from
financial_statements via pit_latest().

Point-in-time correctness
-------------------------
Flow items use the latest four distinct quarterly observations known on the
score date, with the latest annual observation as a fallback. Balance-sheet
items and shares are selected at or before the flow period end. This prevents
mixing one quarter of earnings with a full market capitalisation or pairing
fundamentals from incompatible fiscal periods.

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

from datetime import timedelta
from typing import Optional

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_REQUIRED_FUND_COLS = {"ticker", "period_end_date", "release_date", "period_type", "item_name", "value"}
_REQUIRED_PRICE_COLS = {"ticker", "date", "close"}
_MAX_FUNDAMENTAL_AGE_DAYS = 550


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
        visible = _pit_visible_fundamentals(fundamentals, score_date)
        if visible.empty:
            continue

        # Prices on score_date
        price_snap = (
            prices[prices["date"] == score_date][["ticker", "close"]]
            .assign(close=lambda df: df["close"].astype(float))
            .set_index("ticker")
        )
        if price_snap.empty:
            continue

        # Market cap requires shares_outstanding from fundamentals.
        # NOTE: shares_outstanding is a quarterly filing value and may lag
        # the actual share count by up to 90 days.  This is a known
        # approximation; a more precise source (e.g. daily share counts)
        # would improve accuracy for companies with active buyback programs.
        date_rows = _compute_ratios(visible, price_snap["close"], score_date)
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

def _pit_visible_fundamentals(
    fundamentals: pd.DataFrame,
    as_of_date,
) -> pd.DataFrame:
    """Return PIT-visible rows, keeping the latest filing for each period."""
    visible = fundamentals[fundamentals["release_date"] <= as_of_date]
    period_cutoff = as_of_date - timedelta(days=_MAX_FUNDAMENTAL_AGE_DAYS)
    visible = visible[visible["period_end_date"] >= period_cutoff]
    if visible.empty:
        return visible.copy()
    return (
        visible.sort_values("release_date")
        .drop_duplicates(
            subset=["ticker", "item_name", "period_type", "period_end_date"],
            keep="last",
        )
        .copy()
    )


def _pit_flow_values(visible: pd.DataFrame, item_name: str) -> pd.DataFrame:
    """Return TTM flow values and their fiscal period end per ticker."""
    item_rows = visible[visible["item_name"] == item_name]
    rows: list[dict] = []
    for ticker, ticker_rows in item_rows.groupby("ticker"):
        quarters = (
            ticker_rows[ticker_rows["period_type"] == "quarterly"]
            .sort_values("period_end_date")
            .tail(4)
        )
        if len(quarters) == 4:
            rows.append({
                "ticker": ticker,
                "value": float(quarters["value"].astype(float).sum()),
                "period_end_date": quarters["period_end_date"].max(),
            })
            continue

        annual = (
            ticker_rows[ticker_rows["period_type"] == "annual"]
            .sort_values("period_end_date")
            .tail(1)
        )
        if not annual.empty:
            row = annual.iloc[0]
            rows.append({
                "ticker": ticker,
                "value": float(row["value"]),
                "period_end_date": row["period_end_date"],
            })
    if not rows:
        return pd.DataFrame(columns=["value", "period_end_date"])
    return pd.DataFrame(rows).set_index("ticker")


def _pit_stock_values(
    visible: pd.DataFrame,
    item_name: str,
    anchors: pd.Series,
) -> pd.Series:
    """Return latest stock values at or before each ticker's anchor period."""
    if anchors.empty:
        return pd.Series(dtype=float, name=item_name)

    item_rows = visible[visible["item_name"] == item_name].copy()
    if item_rows.empty:
        return pd.Series(dtype=float, name=item_name)

    item_rows["period_priority"] = (
        item_rows["period_type"] == "quarterly"
    ).astype(int)
    item_rows = (
        item_rows.sort_values(
            ["ticker", "period_end_date", "period_priority", "release_date"]
        )
        .drop_duplicates(["ticker", "period_end_date"], keep="last")
        [["ticker", "period_end_date", "value"]]
    )
    item_rows["period_end_date"] = pd.to_datetime(item_rows["period_end_date"])
    item_rows = item_rows.sort_values(["period_end_date", "ticker"])
    anchor_frame = (
        anchors.rename("anchor_period_end")
        .rename_axis("ticker")
        .reset_index()
    )
    anchor_frame["anchor_period_end"] = pd.to_datetime(
        anchor_frame["anchor_period_end"]
    )
    anchor_frame = anchor_frame.sort_values(["anchor_period_end", "ticker"])
    matched = pd.merge_asof(
        anchor_frame,
        item_rows,
        left_on="anchor_period_end",
        right_on="period_end_date",
        by="ticker",
        direction="backward",
    ).dropna(subset=["value"])
    return matched.set_index("ticker")["value"].astype(float).rename(item_name)


def _compute_ratios(
    visible: pd.DataFrame,
    prices: pd.Series,
    score_date,
) -> list[dict]:
    """Compute per-ticker ratios for a single score date."""
    net_income = _pit_flow_values(visible, "net_income")
    fcf = _pit_flow_values(visible, "free_cash_flow")
    equity_rows = visible[visible["item_name"] == "total_equity"]
    equity_anchors = (
        equity_rows.sort_values("period_end_date")
        .groupby("ticker")["period_end_date"]
        .last()
    )
    total_equity = _pit_stock_values(
        visible, "total_equity", equity_anchors
    )
    earnings_shares = _pit_stock_values(
        visible, "shares_outstanding", net_income["period_end_date"]
    )
    equity_shares = _pit_stock_values(
        visible, "shares_outstanding", equity_anchors
    )
    fcf_shares = _pit_stock_values(
        visible, "shares_outstanding", fcf["period_end_date"]
    )

    rows: list[dict] = []
    for ticker, price in prices.items():
        row: dict = {"ticker": ticker, "date": score_date}
        if price is None or price <= 0:
            continue

        if ticker in net_income.index:
            shares = earnings_shares.get(ticker)
            if shares is not None and shares > 0:
                row["earnings_yield"] = (
                    float(net_income.at[ticker, "value"]) / (price * shares)
                )

        if ticker in total_equity.index and ticker in equity_anchors.index:
            shares = equity_shares.get(ticker)
            if shares is not None and shares > 0:
                row["book_to_market"] = (
                    float(total_equity[ticker]) / (price * shares)
                )

        if ticker in fcf.index:
            shares = fcf_shares.get(ticker)
            if shares is not None and shares > 0:
                row["fcf_yield"] = (
                    float(fcf.at[ticker, "value"]) / (price * shares)
                )

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
