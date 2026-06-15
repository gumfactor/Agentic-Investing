"""Transaction cost estimator for pre-trade and post-trade analysis.

Mirrors the model used in backtesting/engine/fill_simulator.py so that
live trading cost estimates are consistent with backtest assumptions.

Components:
1. Commission: flat per-share (default $0.005)
2. Bid-ask spread: half-spread applied per side (default 5 bps one-way)
3. Market impact: Almgren-Chriss square-root model
   impact_bps = eta * sigma * sqrt(participation_rate)
"""

from __future__ import annotations

import math

import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_COMMISSION_PER_SHARE = 0.005    # USD
_HALF_SPREAD_BPS = 5.0           # bps one-way
_IMPACT_ETA = 0.1                # Almgren-Chriss coefficient
_TRADING_DAYS_PER_YEAR = 252


def estimate_costs(
    ticker: str,
    quantity: float,
    price: float,
    daily_volume: float,
    daily_volatility: float,
    commission_per_share: float = _COMMISSION_PER_SHARE,
    half_spread_bps: float = _HALF_SPREAD_BPS,
    impact_eta: float = _IMPACT_ETA,
) -> dict:
    """Estimate all-in transaction costs for a single order.

    Parameters
    ----------
    quantity:
        Shares to trade (unsigned).
    price:
        Current mid price (USD).
    daily_volume:
        30-day average daily volume in shares.
    daily_volatility:
        Daily return volatility (decimal, not annualized).
    Returns
    -------
    Dict with keys: commission, spread_cost, impact_cost, total_cost,
                    total_cost_bps, participation_rate.
    """
    notional = quantity * price
    participation = quantity / max(daily_volume, 1.0)

    commission = quantity * commission_per_share
    spread_cost = notional * (half_spread_bps / 10_000)
    impact_cost = notional * impact_eta * daily_volatility * math.sqrt(participation)
    total_cost = commission + spread_cost + impact_cost
    total_bps = (total_cost / notional * 10_000) if notional > 0 else 0.0

    return {
        "ticker": ticker,
        "quantity": quantity,
        "price": price,
        "notional": notional,
        "commission": commission,
        "spread_cost": spread_cost,
        "impact_cost": impact_cost,
        "total_cost": total_cost,
        "total_cost_bps": total_bps,
        "participation_rate": participation,
    }


def estimate_batch_costs(
    orders: pd.DataFrame,
    market_data: pd.DataFrame,
    **kwargs: float,
) -> pd.DataFrame:
    """Estimate costs for a batch of orders.

    Parameters
    ----------
    orders:
        DataFrame with columns: ticker, quantity, price.
    market_data:
        DataFrame indexed by ticker with columns: adv_30d, daily_vol.

    Returns
    -------
    DataFrame with cost breakdown per order.
    """
    rows = []
    for _, row in orders.iterrows():
        ticker = row["ticker"]
        md = market_data.loc[ticker] if ticker in market_data.index else None
        adv = float(md["adv_30d"]) if md is not None else 1_000_000.0
        dvol = float(md["daily_vol"]) if md is not None else 0.015
        result = estimate_costs(
            ticker=ticker,
            quantity=float(row["quantity"]),
            price=float(row["price"]),
            daily_volume=adv,
            daily_volatility=dvol,
            **kwargs,
        )
        rows.append(result)

    df = pd.DataFrame(rows)
    logger.info(
        "cost_estimate_batch",
        n_orders=len(df),
        total_cost=round(float(df["total_cost"].sum()), 2),
        avg_cost_bps=round(float(df["total_cost_bps"].mean()), 2),
    )
    return df
