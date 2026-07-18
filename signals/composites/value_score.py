"""Value composite signal: earnings yield, book-to-market, FCF yield.

Blends three value sub-indicators into a single value_score via equal-weight
cross-sectional z-score averaging. Individual sub-scores are also returned so
strategies can reference them independently.

Sub-indicators
--------------
  earnings_yield  : net_income_TTM / market_cap  (high = cheap)
  book_to_market  : total_equity / market_cap     (high = cheap)
  fcf_yield       : free_cash_flow_TTM / market_cap (high = cash-generative)

Point-in-time correctness
-------------------------
Flow items use the latest four distinct quarterly observations known on the
score date, with the latest annual observation as a fallback. Balance-sheet
items and shares are selected at or before the flow period end.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from signals.indicators.value import (
    _compute_ratios,
    _pit_visible_fundamentals,
    _validate_fundamentals,
    _validate_prices,
    _zscore,
)

logger = structlog.get_logger(__name__)


def compute_value_scores(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
    score_dates: Optional[list] = None,
    min_tickers: int = 10,
    eligibility: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Compute cross-sectional value scores at each score date.

    Args:
        fundamentals: Long-format financial_statements rows.  Must include
            items 'net_income', 'total_equity', 'free_cash_flow',
            'shares_outstanding'.
        prices: Long-format daily_prices rows (ticker, date, close).
        score_dates: Dates to compute scores for.  Defaults to all dates
            present in prices.
        min_tickers: Minimum number of tickers with valid fundamental data
            required to compute scores for a date.
        eligibility: optional long-format DataFrame with ``ticker``/``date``
            columns listing the ELIGIBLE (ticker, date) pairs — the
            point-in-time scoring cross-section (BUG-008). Applied BEFORE
            the min_tickers gate and cross-sectional z-scoring so a
            non-member's ratios can never shift members' scores. Dates
            absent from the frame are fully masked (fail closed). ``None``
            keeps the legacy (provisional) behavior.

    Returns:
        Long-format DataFrame with columns:
            ticker, date,
            earnings_yield, book_to_market, fcf_yield,
            value_score  (equal-weight composite of available sub-scores)
    """
    _validate_fundamentals(fundamentals)
    _validate_prices(prices)

    if score_dates is None:
        score_dates = sorted(prices["date"].unique())

    eligible_by_date = None
    if eligibility is not None:
        from signals.composites._eligibility import eligibility_sets_by_date

        eligible_by_date = eligibility_sets_by_date(eligibility)

    rows: list[dict] = []

    for score_date in score_dates:
        visible = _pit_visible_fundamentals(fundamentals, score_date)
        if visible.empty:
            continue

        price_snap = (
            prices[prices["date"] == score_date][["ticker", "close"]]
            .assign(close=lambda df: df["close"].astype(float))
            .set_index("ticker")
        )
        if price_snap.empty:
            continue

        date_rows = _compute_ratios(visible, price_snap["close"], score_date)
        if eligible_by_date is not None:
            # PIT cross-section (BUG-008): only eligible tickers enter the
            # min_tickers gate and the per-date z-scores below.
            eligible = eligible_by_date.get(score_date, set())
            date_rows = [r for r in date_rows if r["ticker"] in eligible]
        if len(date_rows) < min_tickers:
            continue
        rows.extend(date_rows)

    if not rows:
        return pd.DataFrame(
            columns=["ticker", "date", "earnings_yield", "book_to_market", "fcf_yield", "value_score"]
        )

    df = pd.DataFrame(rows)
    sub_cols = [c for c in ["earnings_yield", "book_to_market", "fcf_yield"] if c in df.columns]

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
