"""Total shareholder yield factor.

(Dividends_Per_Share_TTM + Net_Buybacks_Per_Share_TTM) / Price.
The sum of dividend yield and buyback yield — the total cash returned to
equity holders as a fraction of price. Empirically one of the strongest
value signals (Boudoukh et al. 2007, Ibbotson & Kim 2016).
Avoids the channel bias of looking at dividends and buybacks separately;
companies that switch between the two score consistently.
Higher = more total cash returned per dollar invested.

Requires fundamentals columns: dividends_per_share, net_buybacks_per_share
"""
from __future__ import annotations
import pandas as pd
import structlog
from signals.factors._price_utils import validate_prices, to_wide, to_long, cross_sectional_zscore
from signals.factors._fundamentals_utils import (
    validate_fundamentals, fund_to_wide, align_fundamentals,
)

logger = structlog.get_logger(__name__)


def compute_shareholder_yield_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
) -> pd.DataFrame:
    """Cross-sectional z-score of (Dividends + Buybacks) / Price. Higher = more total return."""
    validate_prices(prices)
    validate_fundamentals(fundamentals, {"dividends_per_share", "net_buybacks_per_share"})
    price_wide = to_wide(prices)
    dps = align_fundamentals(fund_to_wide(fundamentals, "dividends_per_share"), price_wide.index)
    nbs = align_fundamentals(fund_to_wide(fundamentals, "net_buybacks_per_share"), price_wide.index)
    yield_ = (dps + nbs) / price_wide.where(price_wide > 0)
    z = cross_sectional_zscore(yield_)
    result = to_long(z, "shareholder_yield_score")
    logger.info("shareholder_yield_scores_computed", dates=result["date"].nunique(), tickers=result["ticker"].nunique())
    return result
