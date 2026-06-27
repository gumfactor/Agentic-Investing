"""21-day VWAP proximity factor.

Score = Close / VWAP(21d), where VWAP = Σ(Close × Volume) / Σ(Volume).
Price above VWAP = buying pressure has dominated over the past month.
Price below VWAP = selling pressure has dominated.
Higher score = price further above volume-weighted average = bullish.

Requires a `volumes` DataFrame in the same long format as prices:
columns [date, ticker, volume] (or [date, ticker, close] with volume in place of close).
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore

logger = structlog.get_logger(__name__)

_WINDOW = 21
_MIN_PERIODS = 10


def compute_price_vs_vwap_21d_scores(
    prices: pd.DataFrame,
    volumes: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of Close / 21-day VWAP. Higher = price above volume-weighted mean.

    Args:
        prices: long-format DataFrame with columns [date, ticker, close].
        volumes: long-format DataFrame with columns [date, ticker, volume].
    """
    validate_prices(prices)
    if volumes is None or volumes.empty:
        raise ValueError("price_vs_vwap_21d requires a non-empty volumes DataFrame")

    price_wide = to_wide(prices)

    vol_col = [c for c in volumes.columns if c not in ("date", "ticker")][0]
    vol_wide = volumes.pivot(index="date", columns="ticker", values=vol_col)
    vol_wide = vol_wide.reindex(index=price_wide.index, columns=price_wide.columns)

    pv = price_wide * vol_wide
    rolling_pv = pv.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    rolling_vol = vol_wide.rolling(_WINDOW, min_periods=_MIN_PERIODS).sum()
    vwap = rolling_pv / rolling_vol.where(rolling_vol > 0)

    ratio = price_wide / vwap.where(vwap > 0)
    z = cross_sectional_zscore(ratio)
    result = to_long(z, "price_vs_vwap_21d_score")
    logger.info("price_vs_vwap_21d_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result
