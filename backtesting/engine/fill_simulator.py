"""Realistic order fill simulation with transaction cost modelling.

Cost model:
  1. Bid-ask spread: buy at mid + half-spread; sell at mid - half-spread.
  2. Market impact: Almgren-Chriss square-root model.
     impact_frac = coeff * sigma_daily * sqrt(participation_rate)
     where participation_rate = order_notional / adv_notional.
  3. Commission: flat per-share rate.

'perfect' fill mode skips all costs (useful for validating logic in isolation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_DAILY_VOL = 0.015   # 1.5% proxy when per-ticker vol is unavailable
_MIN_SHARES = 1e-6


@dataclass(frozen=True)
class Order:
    """A desired weight change for one ticker."""
    ticker: str
    direction: Literal["BUY", "SELL"]
    target_weight: float
    current_weight: float
    delta_weight: float   # target - current (negative for sells)


@dataclass(frozen=True)
class Fill:
    """The result of executing one Order."""
    ticker: str
    direction: str
    shares: float
    fill_price: float
    notional: float          # fill_price * shares
    commission: float
    market_impact: float
    total_cost: float        # commission + market_impact
    sim_date: date


class FillSimulator:
    """Simulates order fills with configurable transaction cost models.

    Args:
        bid_ask_spread_bps: One-way bid-ask spread in basis points.
            Buys execute at mid + spread/2; sells at mid - spread/2.
        market_impact_coeff: Coefficient in the square-root impact model.
        commission_per_share: Flat commission in USD per share.
        fill_model: 'transaction_cost' applies all three cost components;
            'perfect' fills at close with zero cost.
        default_daily_vol: Fallback daily volatility when per-ticker ADV
            is not supplied (used in impact model).
    """

    def __init__(
        self,
        bid_ask_spread_bps: float = 10.0,
        market_impact_coeff: float = 0.5,
        commission_per_share: float = 0.005,
        fill_model: str = "transaction_cost",
        default_daily_vol: float = _DEFAULT_DAILY_VOL,
    ) -> None:
        if fill_model not in ("transaction_cost", "perfect"):
            raise ValueError(f"Unknown fill_model: {fill_model!r}")
        self._spread_bps = bid_ask_spread_bps
        self._impact_coeff = market_impact_coeff
        self._commission = commission_per_share
        self._fill_model = fill_model
        self._default_vol = default_daily_vol

    def simulate_fills(
        self,
        orders: list[Order],
        close_prices: dict[str, float],
        sim_date: date,
        portfolio_value: float,
        adv_shares: Optional[dict[str, float]] = None,
    ) -> list[Fill]:
        """Convert orders into fills.

        Args:
            orders: Weight-delta orders from the portfolio construction step.
            close_prices: Closing price per ticker for sim_date.
            sim_date: Simulation date (used only for record-keeping).
            portfolio_value: Current NAV in USD; used to convert weight deltas
                to notional amounts.
            adv_shares: Average daily volume in shares per ticker. Used in the
                market impact model. If None, a default participation rate is
                assumed.
        Returns:
            List of Fill records; tickers missing from close_prices are skipped.
        """
        fills: list[Fill] = []
        for order in orders:
            close = close_prices.get(order.ticker)
            if close is None or close <= 0:
                logger.warning("fill_skipped_no_price", ticker=order.ticker, date=sim_date)
                continue

            notional = abs(order.delta_weight) * portfolio_value
            shares = notional / close
            if shares < _MIN_SHARES:
                continue

            if self._fill_model == "perfect":
                fill_price = close
                commission = 0.0
                impact = 0.0
            else:
                fill_price = self._apply_spread(close, order.direction)
                commission = shares * self._commission
                impact = self._market_impact(
                    notional, close, order.ticker, adv_shares
                )

            fills.append(Fill(
                ticker=order.ticker,
                direction=order.direction,
                shares=shares,
                fill_price=fill_price,
                notional=fill_price * shares,
                commission=commission,
                market_impact=impact,
                total_cost=commission + impact,
                sim_date=sim_date,
            ))
        return fills

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_spread(self, close: float, direction: str) -> float:
        half = self._spread_bps / 20000.0  # half-spread as fraction
        return close * (1 + half) if direction == "BUY" else close * (1 - half)

    def _market_impact(
        self,
        notional: float,
        close: float,
        ticker: str,
        adv_shares: Optional[dict[str, float]],
    ) -> float:
        if adv_shares and ticker in adv_shares and adv_shares[ticker] > 0:
            adv_notional = adv_shares[ticker] * close
            participation = min(notional / adv_notional, 1.0)
        else:
            participation = 0.05  # default 5% of ADV
        impact_frac = self._impact_coeff * self._default_vol * (participation ** 0.5)
        return notional * impact_frac


def compute_orders(
    target_weights: dict[str, float],
    current_weights: dict[str, float],
    min_trade_weight: float = 1e-4,
) -> list[Order]:
    """Compute the set of orders required to move from current to target weights.

    Args:
        target_weights: Desired portfolio weights (should sum to <= 1.0).
        current_weights: Current portfolio weights.
        min_trade_weight: Weight changes smaller than this are ignored
            to avoid generating tiny round-lot orders.
    Returns:
        List of Orders sorted by sell-first (to free cash before buying).
    """
    all_tickers = sorted(set(target_weights) | set(current_weights))
    sells: list[Order] = []
    buys: list[Order] = []
    for ticker in all_tickers:
        target = target_weights.get(ticker, 0.0)
        current = current_weights.get(ticker, 0.0)
        delta = target - current
        if abs(delta) < min_trade_weight:
            continue
        order = Order(
            ticker=ticker,
            direction="BUY" if delta > 0 else "SELL",
            target_weight=target,
            current_weight=current,
            delta_weight=delta,
        )
        (buys if delta > 0 else sells).append(order)
    # Execute sells first so cash is available for buys
    return sells + buys
