"""Corporate action adjustment utilities.

Computes cumulative price adjustment factors from corporate action records
and applies them to produce split-adjusted and dividend-adjusted prices.

We store unadjusted prices in daily_prices (for auditability) and compute
adjustments on the fly or at batch time. This module handles the computation.

Adjustment methodology:
  - Splits : multiply all prices before the ex-date by (1 / split_ratio).
              e.g., a 2-for-1 split halves all prior prices.
  - Dividends : the 'cumulative adjustment factor' approach divides prior
                prices by (1 - dividend/price_on_ex_date). This produces
                prices as if dividends were reinvested — the standard for
                total return series used in signal computation.
  - Spinoffs  : not yet supported (requires paid data source).

For backtesting, always use adjusted prices derived from this module.
Never use source_adj_close directly for signal computation — its adjustment
methodology may differ from ours and is not auditable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# Precision for adjustment factors — 8 decimal places matches Bloomberg convention.
FACTOR_PRECISION = Decimal("0.00000001")


def compute_adjustment_factors(
    corporate_actions: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.DataFrame:
    """Compute a cumulative price adjustment factor for each (ticker, date).

    Args:
        corporate_actions: DataFrame with columns:
            ticker, ex_date, action_type ('split'|'dividend'), value
        prices: DataFrame with columns:
            ticker, date, close
            Used to look up the closing price on dividend ex-dates.

    Returns:
        DataFrame with columns: ticker, date, adj_factor
        adj_factor is applied as: adjusted_price = unadjusted_price * adj_factor
        adj_factor = 1.0 for dates on or after the most recent action.

    The factor is computed by walking backwards from the most recent date
    so that today's prices are never adjusted — only historical prices change.
    """
    if corporate_actions.empty:
        # No actions: adj_factor = 1.0 everywhere
        if prices.empty:
            return pd.DataFrame(columns=["ticker", "date", "adj_factor"])
        result = prices[["ticker", "date"]].copy()
        result["adj_factor"] = Decimal("1")
        return result

    result_frames = []

    for ticker, price_group in prices.groupby("ticker"):
        ca_group = corporate_actions[corporate_actions["ticker"] == ticker].sort_values("ex_date")

        if ca_group.empty:
            factor_df = price_group[["ticker", "date"]].copy()
            factor_df["adj_factor"] = Decimal("1")
            result_frames.append(factor_df)
            continue

        price_group = price_group.sort_values("date").copy()
        dates = price_group["date"].values
        closes = {row["date"]: row["close"] for _, row in price_group.iterrows()}

        # Build a dict of ex_date -> cumulative multiplier
        # Walking forward through actions, each multiplier applies to all
        # dates strictly before the ex_date.
        multipliers: dict[date, Decimal] = {}

        for _, action in ca_group.iterrows():
            ex_dt = action["ex_date"]
            atype = action["action_type"]
            value = Decimal(str(action["value"]))

            if atype == "split":
                # Prior prices / split_ratio = adjusted price
                # So factor for dates before ex_date = 1 / split_ratio
                if value != 0:
                    multipliers[ex_dt] = Decimal("1") / value
            elif atype == "dividend":
                # Look up the closing price on the ex_date
                ex_close = closes.get(ex_dt)
                if ex_close is not None and ex_close != 0:
                    ex_close_d = Decimal(str(ex_close))
                    # Factor = (close - dividend) / close
                    factor = (ex_close_d - value) / ex_close_d
                    if factor > 0:
                        multipliers[ex_dt] = factor
            # Spinoffs: not implemented in Phase 1
            elif atype == "spinoff":
                logger.warning(
                    "spinoff_not_implemented",
                    ticker=ticker,
                    ex_date=str(ex_dt),
                )

        # Apply multipliers: for each date, the adj_factor is the product
        # of all multipliers for actions with ex_date > date (i.e., things
        # that happened after this date in history, which we must adjust for).
        cum_factor = Decimal("1")
        adj_factors = {}

        # Iterate dates in reverse (newest first); accumulate factors as we
        # pass each ex_date going backwards
        sorted_dates = sorted(dates, reverse=True)
        sorted_ex_dates = sorted(multipliers.keys(), reverse=True)
        ex_idx = 0

        for d in sorted_dates:
            # Accumulate any multipliers with ex_date > d
            while ex_idx < len(sorted_ex_dates) and sorted_ex_dates[ex_idx] > d:
                cum_factor *= multipliers[sorted_ex_dates[ex_idx]]
                ex_idx += 1
            adj_factors[d] = cum_factor.quantize(FACTOR_PRECISION, rounding=ROUND_HALF_UP)

        factor_df = price_group[["ticker", "date"]].copy()
        factor_df["adj_factor"] = factor_df["date"].map(adj_factors).fillna(Decimal("1"))
        result_frames.append(factor_df)

    if not result_frames:
        return pd.DataFrame(columns=["ticker", "date", "adj_factor"])

    return pd.concat(result_frames, ignore_index=True)


def apply_adjustment_factors(
    prices: pd.DataFrame,
    factors: pd.DataFrame,
) -> pd.DataFrame:
    """Merge adjustment factors into a price DataFrame and compute adjusted OHLCV.

    Returns the input DataFrame with additional columns:
        adj_open, adj_high, adj_low, adj_close, adj_factor
    """
    df = prices.merge(factors, on=["ticker", "date"], how="left")
    df["adj_factor"] = df["adj_factor"].fillna(Decimal("1"))

    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[f"adj_{col}"] = df.apply(
                lambda row, c=col: (
                    (Decimal(str(row[c])) * row["adj_factor"]).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP
                    )
                    if row[c] is not None
                    else None
                ),
                axis=1,
            )

    return df
