"""Percentage Price Oscillator (PPO) factor (12/26).

PPO = (EMA(12) - EMA(26)) / EMA(26).
Same as the MACD line but expressed as a percentage of the slow EMA,
making it intrinsically price-normalised and cross-sectionally comparable
without an additional division by price.
Positive = fast EMA above slow EMA = upward trend; higher = stronger.
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.indicators._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore, compute_ema

logger = structlog.get_logger(__name__)


def compute_ppo_12_26_scores(prices: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score of PPO(12,26). Higher = EMA(12) more above EMA(26)."""
    validate_prices(prices)
    wide = to_wide(prices)
    ema_fast = compute_ema(wide, span=12)
    ema_slow = compute_ema(wide, span=26)
    ppo = (ema_fast - ema_slow) / ema_slow.where(ema_slow > 0)
    z = cross_sectional_zscore(ppo)
    result = to_long(z, "ppo_12_26_score")
    logger.info("ppo_12_26_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result
