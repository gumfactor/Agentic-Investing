"""Quality factor: ROE, gross profitability, accruals.

Three complementary quality signals:
  ROE               : net_income / total_equity (Fama-French profitability proxy)
  gross_profitability: gross_profit / total_assets (Novy-Marx 2013)
  accruals          : (net_income - operating_cash_flow) / total_assets
                      Low accruals = high earnings quality → high score

All three are cross-sectionally z-scored.  The composite quality_score is the
equal-weight mean of available sub-scores.

Output sign convention
-----------------------
  roe                 : higher = better
  gross_profitability : higher = better
  accruals            : NEGATED before z-scoring (low accruals = high quality)
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import structlog

from signals.factors.value import _pit_latest_fundamentals, _validate_fundamentals, _validate_prices, _zscore

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

        Accruals is stored as the raw value (net_income - op_cf) / total_assets
        before z-scoring.  The z-score is computed with negation so that
        low-accrual (high-quality) firms receive a high positive score.
    """
    _validate_fundamentals(fundamentals)
    _validate_prices(prices)

    if score_dates is None:
        score_dates = sorted(prices["date"].unique())

    rows: list[dict] = []

    for score_date in score_dates:
        pit_fund = _pit_latest_fundamentals(fundamentals, score_date)
        if not pit_fund:
            continue

        date_rows = _compute_quality_ratios(pit_fund, score_date)
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
            # Negate: lower accruals = higher quality score
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


# ── Internal helpers ───────────────────────────────────────────────────────────

def _compute_quality_ratios(
    pit_fund: dict[str, pd.Series],
    score_date,
) -> list[dict]:
    """Compute per-ticker quality ratios for a single score date."""
    rows: list[dict] = []

    net_income = pit_fund.get("net_income")
    total_equity = pit_fund.get("total_equity")
    total_assets = pit_fund.get("total_assets")
    gross_profit = pit_fund.get("gross_profit")
    op_cf = pit_fund.get("operating_cash_flow")

    # Build the universe from tickers with at least one quality item
    all_tickers: set[str] = set()
    for s in (net_income, total_equity, total_assets, gross_profit, op_cf):
        if s is not None:
            all_tickers.update(s.index)

    for ticker in all_tickers:
        row: dict = {"ticker": ticker, "date": score_date}

        # ROE = net_income / total_equity
        if (
            net_income is not None and ticker in net_income.index
            and total_equity is not None and ticker in total_equity.index
        ):
            eq = float(total_equity[ticker])
            if eq != 0:
                row["roe"] = float(net_income[ticker]) / eq

        # Gross profitability = gross_profit / total_assets
        if (
            gross_profit is not None and ticker in gross_profit.index
            and total_assets is not None and ticker in total_assets.index
        ):
            ta = float(total_assets[ticker])
            if ta > 0:
                row["gross_profitability"] = float(gross_profit[ticker]) / ta

        # Accruals = (net_income - operating_cash_flow) / total_assets
        if (
            net_income is not None and ticker in net_income.index
            and op_cf is not None and ticker in op_cf.index
            and total_assets is not None and ticker in total_assets.index
        ):
            ta = float(total_assets[ticker])
            if ta > 0:
                row["accruals"] = (
                    (float(net_income[ticker]) - float(op_cf[ticker])) / ta
                )

        if len(row) > 2:
            rows.append(row)

    return rows
