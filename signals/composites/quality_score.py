"""Quality composite signal: ROE, gross profitability, accruals.

Blends three quality sub-indicators into a single quality_score via equal-weight
cross-sectional z-score averaging. Individual sub-scores are also returned.

Sub-indicators
--------------
  roe               : net_income / total_equity          (Fama-French profitability)
  gross_profitability: gross_profit / total_assets        (Novy-Marx 2013)
  accruals          : (net_income - operating_cf) / total_assets
                      Hribar & Collins (2002) cash-flow-based measure.
                      NEGATED before z-scoring: low accruals = high earnings quality.

Output sign convention
-----------------------
  roe                 : higher = better
  gross_profitability : higher = better
  accruals            : stored as raw value; negated during z-scoring
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import structlog

from signals.indicators.value import (
    _pit_flow_values,
    _pit_stock_values,
    _pit_visible_fundamentals,
    _validate_fundamentals,
    _validate_prices,
    _zscore,
)

logger = structlog.get_logger(__name__)


def compute_quality_scores(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
    score_dates: Optional[list] = None,
    min_tickers: int = 10,
) -> pd.DataFrame:
    """Compute cross-sectional quality scores at each score date.

    Args:
        fundamentals: Long-format financial_statements rows with items
            'net_income', 'total_equity', 'total_assets', 'gross_profit',
            'operating_cash_flow'.
        prices: Long-format daily_prices rows (ticker, date, close).
            Used only to align the universe; prices are not used in ratio
            computations.
        score_dates: Dates to compute scores for.  Defaults to all dates in prices.
        min_tickers: Minimum tickers required per date.

    Returns:
        Long-format DataFrame with columns:
            ticker, date,
            roe, gross_profitability, accruals,
            quality_score  (equal-weight composite)
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

        date_rows = _compute_quality_ratios(visible, score_date)
        if len(date_rows) < min_tickers:
            continue
        rows.extend(date_rows)

    if not rows:
        return pd.DataFrame(
            columns=["ticker", "date", "roe", "gross_profitability", "accruals", "quality_score"]
        )

    df = pd.DataFrame(rows)
    sub_cols = [c for c in ["roe", "gross_profitability", "accruals"] if c in df.columns]

    for col in sub_cols:
        if col == "accruals":
            df[col] = df.groupby("date")[col].transform(lambda s: _zscore(-s))
        else:
            df[col] = df.groupby("date")[col].transform(_zscore)

    df["quality_score"] = df[sub_cols].mean(axis=1, skipna=True)
    df = df.dropna(subset=sub_cols, how="all")
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

    logger.info(
        "quality_scores_computed",
        dates=df["date"].nunique(),
        tickers=df["ticker"].nunique(),
        sub_factors=sub_cols,
    )
    return df


def _compute_quality_ratios(
    visible: pd.DataFrame,
    score_date,
) -> list[dict]:
    rows: list[dict] = []

    net_income = _pit_flow_values(visible, "net_income")
    gross_profit = _pit_flow_values(visible, "gross_profit")
    op_cf = _pit_flow_values(visible, "operating_cash_flow")
    equity = _pit_stock_values(
        visible, "total_equity", net_income["period_end_date"]
    )
    gross_assets = _pit_stock_values(
        visible, "total_assets", gross_profit["period_end_date"]
    )
    common_flow_anchors = pd.Series({
        ticker: net_income.at[ticker, "period_end_date"]
        for ticker in net_income.index.intersection(op_cf.index)
        if (
            net_income.at[ticker, "period_end_date"]
            == op_cf.at[ticker, "period_end_date"]
        )
    })
    accrual_assets = _pit_stock_values(
        visible, "total_assets", common_flow_anchors
    )

    all_tickers = set(net_income.index) | set(gross_profit.index) | set(op_cf.index)

    for ticker in all_tickers:
        row: dict = {"ticker": ticker, "date": score_date}

        if ticker in net_income.index:
            ticker_equity = equity.get(ticker)
            if ticker_equity is not None and ticker_equity != 0:
                row["roe"] = (
                    float(net_income.at[ticker, "value"]) / ticker_equity
                )

        if ticker in gross_profit.index:
            assets = gross_assets.get(ticker)
            if assets is not None and assets > 0:
                row["gross_profitability"] = (
                    float(gross_profit.at[ticker, "value"]) / assets
                )

        if ticker in net_income.index and ticker in op_cf.index:
            ni_end = net_income.at[ticker, "period_end_date"]
            cf_end = op_cf.at[ticker, "period_end_date"]
            if ni_end == cf_end:
                assets = accrual_assets.get(ticker)
            else:
                assets = None
            if assets is not None and assets > 0:
                row["accruals"] = (
                    (
                        float(net_income.at[ticker, "value"])
                        - float(op_cf.at[ticker, "value"])
                    )
                    / assets
                )

        if len(row) > 2:
            rows.append(row)

    return rows
