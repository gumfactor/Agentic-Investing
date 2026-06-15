"""Pre-trade compliance checks.

Each check is a pure function that returns (passed: bool, reason: str).
The ComplianceEngine runs all checks in sequence; the first failure
transitions the order to REJECTED.

Checks implemented:
  1. Wash-sale guard — no same-ticker sell within 30 days of a buy loss
  2. Position concentration — post-trade weight ≤ max_position_weight
  3. Sector concentration — post-trade sector weight ≤ max_sector_weight
  4. Minimum order size — skip tiny orders below threshold
  5. Circuit-breaker gate — reject all orders when circuit breaker is open
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pandas as pd
import structlog

from execution.oms.order import Order, OrderSide

logger = structlog.get_logger(__name__)

CheckFn = Callable[[Order, dict], tuple[bool, str]]


def _check_wash_sale(order: Order, ctx: dict) -> tuple[bool, str]:
    """Reject sells within 30 days of a loss-realizing buy of the same ticker."""
    if order.side != OrderSide.SELL:
        return True, ""
    recent_buys: dict[str, date] = ctx.get("recent_loss_buys", {})
    buy_date = recent_buys.get(order.ticker)
    if buy_date is None:
        return True, ""
    as_of: date = ctx.get("as_of_date", date.today())
    if (as_of - buy_date).days < 30:
        reason = f"wash-sale: {order.ticker} bought at loss {buy_date}; 30-day lock"
        return False, reason
    return True, ""


def _check_position_concentration(order: Order, ctx: dict) -> tuple[bool, str]:
    """Reject if post-trade single-name weight exceeds the limit."""
    max_weight: float = ctx.get("max_position_weight", 0.05)
    current_weights: pd.Series = ctx.get("current_weights", pd.Series(dtype=float))
    total_nav: float = ctx.get("total_nav", 0.0)

    if total_nav <= 0 or order.limit_price is None:
        return True, ""

    current_w = float(current_weights.get(order.ticker, 0.0))
    trade_w = (order.quantity * order.limit_price) / total_nav

    if order.side == OrderSide.BUY:
        post_trade_w = current_w + trade_w
        # Only block BUYs that push ABOVE the limit
        if post_trade_w > max_weight + 1e-6:
            reason = (
                f"concentration: {order.ticker} post-trade weight {post_trade_w:.2%} "
                f"> limit {max_weight:.2%}"
            )
            return False, reason
    else:
        # SELLs that reduce an already-over-limit position are always allowed
        # (they are risk-reducing); only block sells that somehow increase weight
        post_trade_w = max(0.0, current_w - trade_w)
        if post_trade_w > current_w + 1e-6:
            reason = f"concentration: sell would increase {order.ticker} weight"
            return False, reason

    return True, ""


def _check_sector_concentration(order: Order, ctx: dict) -> tuple[bool, str]:
    """Reject if post-trade sector weight exceeds the limit."""
    max_sector_weight: float = ctx.get("max_sector_weight", 0.25)
    sector_map: dict[str, str] = ctx.get("sector_map", {})
    sector_weights: dict[str, float] = ctx.get("sector_weights", {})
    total_nav: float = ctx.get("total_nav", 0.0)

    if total_nav <= 0 or order.limit_price is None or not sector_map:
        return True, ""

    sector = sector_map.get(order.ticker)
    if sector is None:
        return True, ""

    current_sec_w = sector_weights.get(sector, 0.0)
    trade_w = (order.quantity * order.limit_price) / total_nav
    if order.side == OrderSide.BUY:
        post_sec_w = current_sec_w + trade_w
    else:
        post_sec_w = max(0.0, current_sec_w - trade_w)

    if post_sec_w > max_sector_weight + 1e-6:
        reason = (
            f"sector_concentration: sector={sector} post-trade {post_sec_w:.2%} "
            f"> limit {max_sector_weight:.2%}"
        )
        return False, reason
    return True, ""


def _check_min_order_size(order: Order, ctx: dict) -> tuple[bool, str]:
    """Skip orders below the minimum notional threshold."""
    min_notional: float = ctx.get("min_order_notional", 100.0)
    price = order.limit_price or 0.0
    notional = order.quantity * price
    if notional < min_notional:
        return False, f"below_min_notional:{notional:.2f}<{min_notional}"
    return True, ""


def _check_circuit_breaker(order: Order, ctx: dict) -> tuple[bool, str]:
    """Reject all orders when the circuit breaker is open.

    Defaults to True (open/blocking) when the key is absent — safe-by-default.
    Callers must explicitly pass circuit_breaker_open=False to allow orders through.
    """
    if ctx.get("circuit_breaker_open", True):
        return False, "circuit_breaker_open"
    return True, ""


_DEFAULT_CHECKS: list[CheckFn] = [
    _check_circuit_breaker,
    _check_wash_sale,
    _check_position_concentration,
    _check_sector_concentration,
    _check_min_order_size,
]


class ComplianceEngine:
    """Runs all pre-trade compliance checks against a context dict.

    Parameters
    ----------
    checks:
        Ordered list of check functions to run.  First failure stops evaluation.
    """

    def __init__(self, checks: list[CheckFn] | None = None) -> None:
        self._checks = checks if checks is not None else _DEFAULT_CHECKS

    def check(self, order: Order, context: dict) -> tuple[bool, str]:
        """Return (passed, rejection_reason).

        Parameters
        ----------
        order:
            The order to check.
        context:
            Dictionary with runtime state (nav, weights, sector_map, etc.).
            See individual check functions for expected keys.
        """
        for check_fn in self._checks:
            passed, reason = check_fn(order, context)
            if not passed:
                logger.warning(
                    "compliance_rejected",
                    order_id=order.order_id[:8],
                    ticker=order.ticker,
                    check=check_fn.__name__,
                    reason=reason,
                )
                return False, reason
        return True, ""

    def check_batch(
        self, orders: list[Order], context: dict
    ) -> list[tuple[Order, bool, str]]:
        """Run checks on every order; return list of (order, passed, reason)."""
        return [(o, *self.check(o, context)) for o in orders]
